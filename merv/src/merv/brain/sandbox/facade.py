"""Stable Sandbox facade over typed command, query, and maintenance handlers."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import sandbox_views
from ..kernel.env import env_float
from ..kernel.ports.quota_admission import QuotaAdmission
from ..kernel.ports.sandbox_worker import SandboxWorker
from ..kernel.state.store import Connection
from ..kernel.utils import ValidationError
from .commands import SandboxCommandHandler
from .queries import SandboxQueryHandler
from .runtime import SandboxRuntime
from .sandbox_backend import BackendCapabilities, BackendValidationError
from .sandbox_heartbeat import SandboxActivityPolicy
from .sandbox_support import DEFAULT_REQUEST_WAIT_SECONDS, RUNS_WAIT_POLL_SECONDS


class SandboxFacade:
    """Small stable API; handlers own lifecycle policy and read assembly."""

    def __init__(
        self,
        *,
        worker: SandboxWorker,
        runtime: SandboxRuntime,
        request_wait_seconds: float | None = None,
        quotas: QuotaAdmission | None = None,
        storage_enabled: bool = False,
        storage_hint: str = "",
        attachment_check: Callable[..., None] | None = None,
    ) -> None:
        if quotas is None:
            raise ValidationError("quotas is required")
        if not callable(getattr(quotas, "check_admission", None)):
            raise ValidationError("quotas.check_admission is required")
        if not callable(getattr(quotas, "check_lifetime_extension", None)):
            raise ValidationError("quotas.check_lifetime_extension is required")
        self.quotas = quotas
        self.worker = worker
        self.storage_enabled = bool(storage_enabled)
        self.storage_hint = str(storage_hint or "")
        self.attachment_check = attachment_check
        self.activity_policy = SandboxActivityPolicy()
        self.request_wait_seconds = env_float(
            "RESEARCH_PLUGIN_SANDBOX_REQUEST_WAIT",
            request_wait_seconds,
            DEFAULT_REQUEST_WAIT_SECONDS,
        )
        self.runtime = runtime
        self.repository = runtime.repository
        self.store = self.repository.store
        self.metrics = runtime.metrics
        self.runs_ledger = runtime.runs
        self.runs_wait_poll_seconds = RUNS_WAIT_POLL_SECONDS
        self._secrets_delivered: set[str] = set()
        self.lifecycle = runtime.lifecycle
        self.backend = self.lifecycle.backend
        self.mgmt_keys = self.lifecycle.mgmt_keys
        self.tasks = self.lifecycle.tasks
        self.provisioner = runtime.provisioner
        self.daemons = runtime.daemons
        self.transcript_cache = runtime.transcripts
        self.commands = SandboxCommandHandler(self)
        self.queries = SandboxQueryHandler(self)

    def _deliver_secrets_once(
        self, *, row: dict[str, Any], experiment_id: str
    ) -> None:
        uid = str(row.get("sandbox_uid") or "")
        if not uid or row.get("status") != "running" or uid in self._secrets_delivered:
            return
        self._deliver_secrets(row=row, experiment_id=experiment_id)
        self._secrets_delivered.add(uid)

    def _deliver_secrets(self, *, row: dict[str, Any], experiment_id: str) -> None:
        if row.get("status") != "running":
            return
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id:
            return
        try:
            secrets = self.backend.sandbox_secrets()
        except Exception:
            secrets = {}
        if not secrets:
            return
        with suppress(Exception):
            self.backend.write_secrets(
                sandbox_id=sandbox_id,
                secrets=secrets,
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                key_path=str(self._mgmt_key_path(row=row)),
            )

    def _mgmt_key_path(self, *, row: dict[str, Any]) -> Path:
        return self.mgmt_keys.key_path(sandbox_uid=str(row.get("sandbox_uid") or ""))

    def _agent_result(
        self,
        *,
        row: dict[str, Any],
        reused: bool | None,
        include_data_plane_enrichment: bool,
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        row = self._with_active_experiment_ids(row=row)
        view = (
            self._agent_view(
                row=row, reused=reused, use_sandbox_uid_command=use_sandbox_uid_command
            )
            if include_data_plane_enrichment
            else self._agent_facts(row=row, reused=reused)
        )
        return self._with_runs_nudge(
            view=view, sandbox_uid=str(row.get("sandbox_uid") or "")
        )

    def _with_runs_nudge(
        self, *, view: dict[str, Any], sandbox_uid: str
    ) -> dict[str, Any]:
        if not sandbox_uid:
            return view
        try:
            nudge = self.runs_ledger.nudge_line(sandbox_uid=sandbox_uid)
        except Exception:
            return view
        if nudge:
            view["runs"] = nudge
        return view

    def _agent_facts(
        self, *, row: dict[str, Any], reused: bool | None
    ) -> dict[str, Any]:
        return sandbox_views.agent_row_facts(
            row=row,
            env_info=self._sandbox_environment(),
            reused=reused,
            storage_enabled=self.storage_enabled,
        )

    def _agent_view(
        self,
        *,
        row: dict[str, Any],
        reused: bool | None,
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        sandbox_uid = str(row.get("sandbox_uid") or "")
        view_name = f"sandbox-{sandbox_uid[:12]}"
        facts = self._agent_facts(row=row, reused=reused)
        enrichment = self.worker.sandbox_enrichment(
            row=row, name=view_name, use_sandbox_uid_command=use_sandbox_uid_command
        )
        return sandbox_views.merge_agent_view(
            facts=facts, enrichment=enrichment, storage_hint=self.storage_hint
        )

    def _agent_summary(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return sandbox_views.agent_summary(
            row=self._with_active_experiment_ids(row=row)
        )

    def _row_view(
        self, *, row: dict[str, Any], conn: Connection | None = None
    ) -> dict[str, Any]:
        row = self._with_active_experiment_ids(row=row)
        sandbox_uid = str(row.get("sandbox_uid") or "")
        local_key = sandbox_uid
        local_name = f"sandbox-{sandbox_uid[:12]}"
        return sandbox_views.sandbox_row_view(
            row=row,
            local_sync_dir=str(
                self.worker.local_experiment_dir(
                    experiment_id=local_key, name=local_name
                )
            ),
        )

    def _active_experiment_ids_for_row(self, *, row: dict[str, Any]) -> list[str]:
        raw = row.get("active_experiment_ids")
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item)]
        sandbox_uid = str(row.get("sandbox_uid") or "")
        if not sandbox_uid:
            return []
        return self.repository.active_experiment_ids(sandbox_uid=sandbox_uid)

    def _with_active_experiment_ids(self, *, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        active = self._active_experiment_ids_for_row(row=row)
        out["active_experiment_ids"] = active
        if not out.get("experiment_id") and active:
            out["experiment_id"] = active[0]
        return out

    def _capabilities_for(self, *, provider: str | None) -> BackendCapabilities:
        try:
            return self.backend.capabilities_for(provider=provider)
        except BackendValidationError as exc:
            raise ValidationError(str(exc)) from exc

    def _price_for_instance(
        self,
        *,
        instance_type: str | None,
        region: str | None,
        provider: str | None = None,
    ) -> float | None:
        if not instance_type:
            return None
        try:
            catalog = self.backend.hardware_catalog(region=region)
        except Exception:
            return None
        if not catalog:
            return None
        for option in catalog.get("options", []) or []:
            if str(option.get("instance_type") or "") != instance_type:
                continue
            tagged = str(option.get("provider") or "")
            if provider and tagged and (tagged != provider):
                continue
            price = option.get("price_usd_per_hour")
            return float(price) if price is not None else None
        return None

    def _hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        catalog = self.backend.hardware_catalog(gpu=gpu, region=region)
        if catalog is None:
            return {
                "provider": self.backend.capabilities.name,
                "selection_required": False,
                "options": [],
                "regions": [],
            }
        return catalog

    def _sandbox_environment(self) -> dict[str, Any]:
        try:
            result = self.backend.sandbox_environment()
        except Exception:
            return {"available_tokens": [], "notes": []}
        if not isinstance(result, dict):
            return {"available_tokens": [], "notes": []}
        tokens = [
            str(token)
            for token in result.get("available_tokens", [])
            if isinstance(token, str) and token
        ]
        notes = [
            str(note)
            for note in result.get("notes", [])
            if isinstance(note, str) and note
        ]
        return {"available_tokens": tokens, "notes": notes}

    def request(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        gpu: str | None = None,
        cpu: float | None = None,
        memory: int | None = None,
        time_limit: int | None = None,
        instance_type: str | None = None,
        region: str | None = None,
        provider: str | None = None,
        public_key: str | None = None,
        public_key_override: str | None = None,
        include_data_plane_enrichment: bool = True,
        additional: bool = False,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        return self.commands.execute_request(
            experiment_id=experiment_id,
            project_id=project_id,
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            instance_type=instance_type,
            region=region,
            provider=provider,
            public_key=public_key,
            public_key_override=public_key_override,
            include_data_plane_enrichment=include_data_plane_enrichment,
            additional=additional,
            sandbox_uid=sandbox_uid,
        )

    def request_from_data_plane(
        self,
        *,
        experiment_id: str | None = None,
        public_key: str,
        project_id: str | None = None,
        gpu: str | None = None,
        cpu: float | None = None,
        memory: int | None = None,
        time_limit: int | None = None,
        instance_type: str | None = None,
        region: str | None = None,
        provider: str | None = None,
        additional: bool = False,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        return self.request(
            experiment_id=experiment_id,
            project_id=project_id,
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            instance_type=instance_type,
            region=region,
            provider=provider,
            public_key_override=public_key,
            include_data_plane_enrichment=False,
            additional=additional,
            sandbox_uid=sandbox_uid,
        )

    def get(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
        include_data_plane_enrichment: bool = True,
    ) -> dict[str, Any]:
        return self.queries.execute_get(
            experiment_id=experiment_id,
            project_id=project_id,
            tenant_id=tenant_id,
            sandbox_uid=sandbox_uid,
            include_data_plane_enrichment=include_data_plane_enrichment,
        )

    def attach(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        sandbox_uid: str,
        include_data_plane_enrichment: bool = True,
        public_key_override: str | None = None,
    ) -> dict[str, Any]:
        return self.commands.execute_attach(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
            include_data_plane_enrichment=include_data_plane_enrichment,
            public_key_override=public_key_override,
        )

    def attach_from_data_plane(
        self,
        *,
        experiment_id: str,
        sandbox_uid: str,
        public_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return self.attach(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
            public_key_override=public_key,
            include_data_plane_enrichment=False,
        )

    def extend(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
        seconds: int = 1800,
    ) -> dict[str, Any]:
        return self.commands.execute_extend(
            experiment_id=experiment_id,
            project_id=project_id,
            tenant_id=tenant_id,
            sandbox_uid=sandbox_uid,
            seconds=seconds,
        )

    def options(
        self,
        *,
        project_id: str | None = None,
        gpu: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        return self.queries.execute_options(
            project_id=project_id, gpu=gpu, region=region
        )

    def list_sandboxes(self, *, project_id: str | None = None) -> dict[str, Any]:
        return self.queries.execute_list(project_id=project_id)

    def release(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
        confirm_retained: bool = False,
    ) -> dict[str, Any]:
        return self.commands.execute_release(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
            confirm_retained=confirm_retained,
        )

    def terminal(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
        tail: int | None = None,
        since: int | None = None,
    ) -> dict[str, Any]:
        return self.queries.execute_terminal(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
            tail=tail,
            since=since,
        )

    def runs(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        return self.queries.execute_runs(
            experiment_id=experiment_id,
            project_id=project_id,
            tenant_id=tenant_id,
            sandbox_uid=sandbox_uid,
            wait_seconds=wait_seconds,
        )

    def health(self) -> dict[str, Any]:
        return self.queries.health()

    def get_row(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any] | None:
        return self.queries.get_row(
            experiment_id=experiment_id, project_id=project_id, sandbox_uid=sandbox_uid
        )

    def rows(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        return self.queries.rows(project_id=project_id)

    def row_view(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return self.queries.row_view(row=row)

    def backend_health(self) -> dict[str, Any]:
        return self.queries.backend_health()

    def project_signal(self, *, project_id: str) -> str:
        """Stable cache token for project sandbox rows."""
        return self.store.project_sandbox_signal(project_id=project_id)

    def sample_metrics(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        return self.queries.sample_metrics(
            experiment_id=experiment_id, project_id=project_id, sandbox_uid=sandbox_uid
        )

    def for_experiment(
        self, *, project_id: str, experiment_id: str
    ) -> list[dict[str, Any]]:
        with self.store.transaction() as conn:
            self.store.require_project_id(conn=conn, project_id=project_id)
            return self.queries.sandboxes_for_experiment(
                conn=conn, project_id=project_id, experiment_id=experiment_id
            )

    def for_project(self, *, project_id: str) -> list[dict[str, Any]]:
        with self.store.transaction() as conn:
            self.store.require_project_id(conn=conn, project_id=project_id)
            return self.queries.sandboxes_for_project(
                conn=conn, project_id=project_id
            )

    def reap_expired(self, **kwargs: Any) -> int:
        return self.lifecycle.reap_expired(**kwargs)

    def reap_idle(self, **kwargs: Any) -> int:
        return self.daemons.reap_idle(**kwargs)

    def reconcile_running_rows(self) -> int:
        left_running = 0
        for row in self.repository.list_running_rows():
            try:
                fresh = self.lifecycle.reconcile(row=row)
            except Exception:
                continue
            if (fresh or {}).get("status") != "running":
                left_running += 1
        return left_running

    def reap_stale_provisions(self, *, now: datetime, deadline_seconds: float) -> int:
        return self.provisioner.reap_stale_provisions(
            now=now, deadline_seconds=deadline_seconds
        )


SandboxService = SandboxFacade

__all__ = ["SandboxFacade", "SandboxService"]
