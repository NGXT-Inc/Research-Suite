"""Stable Sandbox facade over lifecycle collaborators and typed queries."""

from __future__ import annotations

import shlex
import threading
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from . import sandbox_views
from ..kernel.env import env_float
from ..kernel.ports.quota_admission import AdmissionRequest, QuotaAdmission
from ..kernel.ports.sandbox_worker import SandboxWorker
from ..kernel.state.store import Connection
from ..kernel.utils import NotFoundError, ValidationError, format_iso, parse_iso
from .lifecycle_reducer import release_decision
from .queries import SandboxQueryHandler
from .runtime import SandboxRuntime
from .sandbox_backend import BackendCapabilities, BackendValidationError, SandboxRequest
from .sandbox_heartbeat import SandboxActivityPolicy
from .sandbox_paths import remote_experiment_dir
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    DEFAULT_REQUEST_WAIT_SECONDS,
    MAX_TIME_LIMIT_SECONDS,
    RUNS_WAIT_POLL_SECONDS,
    validate_request_inputs,
)


# Default retained outputs pulled when a caller passes no explicit paths
# (mirrors the sandbox.pull_outputs contract description).
_DEFAULT_PULL_OUTPUTS = (
    "results",
    "figures",
    "report.md",
    "graph.json",
    "metrics.json",
    "results.json",
)

# Command template a non-local (key) caller runs itself to pull outputs over
# SSH/rsync. host/port/user/remote-path are filled from sandbox facts; the
# caller fills <key_path> (its own private key) and <local-destination>. The
# brain never runs rsync or touches local files (no-dataplane Phase C).
_RSYNC_PULL_OUTPUTS_TEMPLATE = (
    "rsync -az --itemize-changes --no-links --no-devices --no-specials "
    '-e "ssh -i <key_path> -p {port} -o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null" {remote_sources} <local-destination>'
)


def _pull_output_sources(
    *, remote_dir: str, user: str, host: str, paths: list[str]
) -> list[str]:
    """Validated, individually shell-quoted rsync remote source arguments."""
    root = PurePosixPath(remote_dir)
    sources: list[str] = []
    for raw_path in paths:
        path = str(raw_path).strip()
        relative = PurePosixPath(path)
        if not path or relative.is_absolute() or ".." in relative.parts:
            raise ValidationError(
                "sandbox.pull_outputs paths must be non-empty relative paths "
                "without '..' components"
            )
        resolved = root.joinpath(relative)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValidationError(
                f"sandbox.pull_outputs path escapes experiment_dir: {path}"
            ) from exc
        sources.append(shlex.quote(f"{user}@{host}:{resolved}"))
    return sources


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
        # Per-sandbox HF token resolved at request() from the provisioning user,
        # held in memory until the VM/SSH post-boot secret delivery reads it (the
        # delivery often lands on a later sandbox.get, once the box is running).
        # Never persisted — write-only per no-dataplane Phase C ruling 7.
        self._provision_hf_tokens: dict[str, str] = {}
        # Per-experiment lock serializing the reserve/provision DECISION so two
        # concurrent sandbox.request calls for one experiment cannot each mint a
        # fresh sandbox_uid and double-provision (the TOCTOU guard, Phase C).
        self._request_locks: dict[str, threading.Lock] = {}
        self._request_locks_guard = threading.Lock()
        self.lifecycle = runtime.lifecycle
        self.backend = self.lifecycle.backend
        self.mgmt_keys = self.lifecycle.mgmt_keys
        self.tasks = self.lifecycle.tasks
        self.provisioner = runtime.provisioner
        self.daemons = runtime.daemons
        self.transcript_cache = runtime.transcripts
        self.queries = SandboxQueryHandler(self)

    def _deliver_secrets_once(
        self, *, row: dict[str, Any], experiment_id: str
    ) -> None:
        uid = str(row.get("sandbox_uid") or "")
        if not uid or row.get("status") != "running" or uid in self._secrets_delivered:
            return
        self._deliver_secrets(row=row, experiment_id=experiment_id)
        self._secrets_delivered.add(uid)
        self._provision_hf_tokens.pop(uid, None)

    def _deliver_secrets(self, *, row: dict[str, Any], experiment_id: str) -> None:
        if row.get("status") != "running":
            return
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id:
            return
        # The provisioning user's HF token (empty for backends that inject at
        # provision, like Modal, or when the user set no token).
        hf_token = self._provision_hf_tokens.get(str(row.get("sandbox_uid") or ""), "")
        try:
            secrets = self.backend.sandbox_secrets(hf_token=hf_token)
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

    @contextmanager
    def _experiment_request_guard(self, experiment_id: str) -> Iterator[None]:
        """Serialize the reserve/provision decision for one experiment.

        Two concurrent sandbox.request calls for the same experiment with no
        active sandbox each read ``existing=None``, mint a DIFFERENT fresh
        sandbox_uid, and start two provision jobs — a double-provision (double
        billing). Serializing the decision makes the second caller read the
        first's just-inserted provisioning row and single-flight onto its uid.
        Released BEFORE the (up to 45s) provisioning wait, so a second caller
        blocks only on the brief decision window. Standalone requests (no
        experiment_id) carry no shared identity to dedup on — not serialized."""
        if not experiment_id:
            yield
            return
        with self._request_locks_guard:
            lock = self._request_locks.get(experiment_id)
            if lock is None:
                lock = threading.Lock()
                self._request_locks[experiment_id] = lock
        with lock:
            yield

    def _resolve_hf_token(self, *, user_id: str) -> str:
        """The provisioning user's Hugging Face token, or '' (public models).

        Reads the write-only per-user token store. Best-effort: a lookup
        failure degrades to no token rather than failing the provision."""
        if not user_id:
            return ""
        try:
            return self.store.user_hf_token(user_id=user_id)
        except Exception:
            return ""

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
        provisioning_user_id: str = "",
        provisioning_key_id: str = "",
    ) -> dict[str, Any]:
        experiment_id = (experiment_id or "").strip()
        provider = (provider or "").strip() or None
        caps = self._capabilities_for(provider=provider)
        gpu, cpu, memory, time_limit = validate_request_inputs(
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            configurable_resources=caps.configurable_resources,
        )
        instance_type = (instance_type or "").strip() or None
        region = (region or "").strip() or None
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
        if experiment_id and self.attachment_check is not None:
            self.attachment_check(attachment_id=experiment_id, project_id=project_id)
        # Serialize the reserve/provision decision per experiment (TOCTOU guard,
        # Phase C); released before the provisioning wait below.
        with self._experiment_request_guard(experiment_id):
            if experiment_id:
                try:
                    existing = self.repository.load_row(experiment_id=experiment_id)
                except NotFoundError:
                    existing = None
            else:
                existing = None
                additional = False
            requested_uid = (sandbox_uid or "").strip()
            sandbox_uid = requested_uid or (
                self.repository.new_sandbox_uid()
                if additional
                else str(
                    (existing or {}).get("sandbox_uid")
                    or self.repository.new_sandbox_uid()
                )
            )
            supplied_public_key = (
                str(public_key_override).strip()
                if public_key_override is not None
                else str(public_key or "").strip()
            )
            if not supplied_public_key:
                raise ValidationError(
                    "sandbox.request requires public_key; generate a caller-owned OpenSSH keypair and pass the single-line .pub contents"
                )
            public_key = supplied_public_key
            public_key_source = "caller"
            management_public_key = self.mgmt_keys.ensure(sandbox_uid=sandbox_uid)
            if (
                not additional
                and existing
                and (existing.get("status") in ACTIVE_SANDBOX_STATUSES)
                and existing.get("sandbox_id")
                and (
                    self.lifecycle.liveness(sandbox_id=str(existing["sandbox_id"]))
                    is not False
                )
            ):
                self.repository.touch_alive(
                    experiment_id=experiment_id,
                    sandbox_uid=str(existing.get("sandbox_uid") or ""),
                )
                row = self.lifecycle.refresh_endpoint(
                    row=self.repository.get_by_uid(
                        sandbox_uid=str(existing.get("sandbox_uid") or "")
                    )
                )
                self.repository.emit_event(
                    project_id=project_id,
                    event_type="sandbox.reused",
                    experiment_id=experiment_id,
                    payload={
                        "sandbox_id": existing["sandbox_id"],
                        "sandbox_uid": existing.get("sandbox_uid", ""),
                        "active_experiment_ids": self.repository.active_experiment_ids(
                            sandbox_uid=str(existing.get("sandbox_uid") or "")
                        ),
                    },
                )
                result = self._agent_result(
                    row=row,
                    reused=True,
                    include_data_plane_enrichment=include_data_plane_enrichment,
                    use_sandbox_uid_command=True,
                )
                result["public_key_source"] = public_key_source
                return result
            if caps.requires_hardware_selection and (not instance_type):
                catalog = self._hardware_catalog(gpu=gpu, region=region)
                return sandbox_views.needs_selection_view(
                    experiment_id=experiment_id, project_id=project_id, catalog=catalog
                )
            self.quotas.check_admission(
                request=AdmissionRequest(
                    tenant_id=self.repository.tenant_for_project(project_id=project_id),
                    time_limit_seconds=int(time_limit),
                    price_usd_per_hour=self._price_for_instance(
                        instance_type=instance_type, region=region, provider=caps.name
                    ),
                )
            )
            remote_dir = remote_experiment_dir(
                experiment_id=sandbox_uid, name=f"sandbox-{sandbox_uid[:12]}"
            )
            # Resolve the provisioning user's Hugging Face token (no-dataplane
            # Phase C): Modal injects it at provision from req.hf_token; VM/SSH
            # backends read it from the stash at post-boot delivery. Empty =>
            # public models only. The token value is never persisted on the row.
            hf_token = self._resolve_hf_token(user_id=provisioning_user_id)
            if hf_token:
                self._provision_hf_tokens[sandbox_uid] = hf_token
            req = SandboxRequest(
                experiment_id=sandbox_uid,
                project_id=project_id,
                public_key=public_key,
                sandbox_uid=sandbox_uid,
                management_public_key=management_public_key,
                management_key_path=str(
                    self.mgmt_keys.key_path(sandbox_uid=sandbox_uid)
                ),
                gpu=gpu,
                cpu=cpu,
                memory=memory,
                time_limit=time_limit,
                instance_type=instance_type,
                region=region,
                provider=provider,
                remote_workdir=remote_dir,
                public_key_source=public_key_source,
                hf_token=hf_token,
                key_id=str(provisioning_key_id or ""),
            )
            job = self.provisioner.ensure_job(
                experiment_id=experiment_id,
                project_id=project_id,
                req=req,
                existing=None if additional else existing,
                sandbox_uid=sandbox_uid,
                create_new=additional,
            )
        job.done.wait(timeout=self.request_wait_seconds)
        row = self.repository.get_by_uid(sandbox_uid=sandbox_uid)
        reused = False if row.get("status") == "running" else None
        self._deliver_secrets_once(row=row, experiment_id=experiment_id)
        result = self._agent_result(
            row=row,
            reused=reused,
            include_data_plane_enrichment=include_data_plane_enrichment,
            use_sandbox_uid_command=True,
        )
        result["public_key_source"] = public_key_source
        return result

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
        provisioning_user_id: str = "",
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
            provisioning_user_id=provisioning_user_id,
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
        _ = public_key_override
        sandbox_uid = sandbox_uid.strip()
        if not sandbox_uid:
            raise ValidationError("sandbox.attach requires sandbox_uid")
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
        try:
            source_row = self.repository.get_by_uid(sandbox_uid=sandbox_uid)
        except NotFoundError as exc:
            raise NotFoundError(f"sandbox not found: {sandbox_uid}") from exc
        if source_row.get("project_id") != project_id:
            raise NotFoundError(
                f"sandbox not found in project {project_id}: {sandbox_uid}"
            )
        source_row = self.lifecycle.reconcile(row=source_row)
        if source_row.get("status") != "running" or not source_row.get("sandbox_id"):
            raise ValidationError("sandbox.attach requires a running sandbox")
        if self.lifecycle.liveness(sandbox_id=str(source_row["sandbox_id"])) is False:
            raise ValidationError("sandbox.attach requires a live sandbox")
        if self.attachment_check is not None:
            self.attachment_check(attachment_id=experiment_id, project_id=project_id)
        row = self.repository.attach(
            sandbox_uid=sandbox_uid, experiment_id=experiment_id, project_id=project_id
        )
        active_experiment_ids = self.repository.active_experiment_ids(
            sandbox_uid=sandbox_uid
        )
        self.repository.emit_event(
            project_id=project_id,
            event_type="sandbox.attached",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": row.get("sandbox_id", ""),
                "sandbox_uid": sandbox_uid,
                "active_experiment_ids": active_experiment_ids,
            },
        )
        result = self._agent_result(
            row=row,
            reused=True,
            include_data_plane_enrichment=include_data_plane_enrichment,
            use_sandbox_uid_command=True,
        )
        result["active_experiment_ids"] = active_experiment_ids
        return result

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
        experiment_id = (experiment_id or "").strip()
        sandbox_uid = (sandbox_uid or "").strip()
        if not experiment_id and (not sandbox_uid):
            raise ValidationError(
                "sandbox.extend requires experiment_id or sandbox_uid"
            )
        seconds = int(seconds)
        if seconds <= 0 or seconds > 1800:
            raise ValidationError("sandbox.extend seconds must be between 1 and 1800")
        row = self.repository.fetch_scoped(
            experiment_id=experiment_id,
            project_id=project_id,
            tenant_id=tenant_id,
            sandbox_uid=sandbox_uid,
        )
        caps = self._capabilities_for(provider=str(row.get("provider") or "") or None)
        if not caps.lifetime_extension_supported:
            raise ValidationError(
                f"{caps.name} sandboxes do not support lifetime extension"
            )
        row = self.lifecycle.reconcile(row=row)
        if row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            raise ValidationError("sandbox.extend requires a running sandbox")
        expires_at = parse_iso(row.get("expires_at"))
        if expires_at is None:
            raise ValidationError(
                "sandbox.extend requires an existing expires_at deadline"
            )
        current_limit = int(row.get("time_limit") or 0)
        new_limit = current_limit + seconds
        if new_limit > MAX_TIME_LIMIT_SECONDS:
            raise ValidationError(
                f"sandbox.extend would exceed the max lifetime ({MAX_TIME_LIMIT_SECONDS}s)"
            )
        resolved_project_id = str(row.get("project_id") or project_id or "")
        tenant = str(
            row.get("tenant_id")
            or self.repository.tenant_for_project(project_id=resolved_project_id)
        )
        price = row.get("price_usd_per_hour")
        self.quotas.check_lifetime_extension(
            tenant_id=tenant,
            total_time_limit_seconds=new_limit,
            price_usd_per_hour=float(price) if price is not None else None,
        )
        if not self.activity_policy.is_active_snapshot(
            snapshot=self.repository.heartbeat_snapshot(row=row),
            command=self.repository.command_snapshot(row=row),
        ):
            raise ValidationError(
                "sandbox.extend requires a running command or active heartbeat metrics"
            )
        old_expires_at = str(row.get("expires_at") or "")
        new_expires_at = format_iso(expires_at + timedelta(seconds=seconds))
        updated = self.repository.extend_lifetime(
            sandbox_uid=str(row.get("sandbox_uid") or ""),
            expires_at=new_expires_at,
            time_limit=new_limit,
        )
        resolved_experiment_id = experiment_id or str(
            updated.get("experiment_id") or ""
        )
        self.repository.emit_event(
            project_id=resolved_project_id,
            event_type="sandbox.lifetime_extended",
            experiment_id=resolved_experiment_id,
            payload={
                "sandbox_id": updated.get("sandbox_id", ""),
                "sandbox_uid": updated.get("sandbox_uid", ""),
                "old_expires_at": old_expires_at,
                "expires_at": new_expires_at,
                "seconds": seconds,
                "time_limit": new_limit,
            },
        )
        view = self._agent_result(
            row=updated,
            reused=None,
            include_data_plane_enrichment=False,
            use_sandbox_uid_command=True,
        )
        view["extended"] = True
        view["old_expires_at"] = old_expires_at
        view["extended_by_seconds"] = seconds
        view["time_limit"] = new_limit
        return view

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
        experiment_id = (experiment_id or "").strip()
        if not experiment_id and (not (sandbox_uid or "").strip()):
            raise ValidationError(
                "sandbox.release requires experiment_id or sandbox_uid"
            )
        row = self.repository.fetch_scoped(
            experiment_id=experiment_id, project_id=project_id, sandbox_uid=sandbox_uid
        )
        targets = [row]
        if experiment_id and (not sandbox_uid):
            rows = [
                item
                for item in self.repository.list_by_experiment(
                    experiment_id=experiment_id
                )
                if item.get("project_id") == row.get("project_id")
            ]
            active = [
                item
                for item in rows
                if item.get("status") in ACTIVE_SANDBOX_STATUSES | {"provisioning"}
            ]
            if len(active) > 1:
                targets = active
        if not confirm_retained:
            return self._with_runs_nudge(
                view=self._release_confirmation(
                    experiment_id=experiment_id,
                    project_id=str(row.get("project_id") or ""),
                    targets=targets,
                ),
                sandbox_uid=str(row.get("sandbox_uid") or ""),
            )
        views = [self._release_row(row=target) for target in targets]
        if len(views) == 1:
            return views[0]
        return {
            "experiment_id": experiment_id,
            "project_id": row.get("project_id"),
            "status": "terminated",
            "released_count": len(views),
            "sandboxes": views,
            "hint": "All live sandboxes for this experiment were terminated.",
        }

    def _release_confirmation(
        self, *, experiment_id: str, project_id: str, targets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        pending = [
            {
                "sandbox_uid": str(target.get("sandbox_uid") or ""),
                "sandbox_id": str(target.get("sandbox_id") or ""),
                "status": target.get("status"),
                "workdir": target.get("workdir"),
            }
            for target in targets
        ]
        count = len(pending)
        noun = "sandbox" if count == 1 else "sandboxes"
        return {
            "experiment_id": experiment_id,
            "project_id": project_id,
            "status": "confirmation_required",
            "released": False,
            "pending_release": pending,
            "hint": f"Not released yet. This will permanently destroy {count} {noun} and everything on the VM. First confirm you have retained everything you need: rsync the light files you want off the box yourself over SSH into the local work folder"
            + (
                f", and storage.submit for durable heavy artifacts. {self.storage_hint}"
                if self.storage_enabled
                else "; heavy-file storage is not enabled on this backend"
            )
            + ". Nothing is copied automatically — anything you do not pull is lost. When you have everything, re-call sandbox.release with confirm_retained=true to terminate.",
        }

    def _release_row(self, *, row: dict[str, Any]) -> dict[str, Any]:
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        self.provisioner.cancel(experiment_id=experiment_id, sandbox_uid=sandbox_uid)
        was_active = bool(
            row.get("sandbox_id") and row.get("status") in ACTIVE_SANDBOX_STATUSES
        )
        outcome = self.lifecycle.terminate_vm(
            row=row,
            try_direct=bool(
                row.get("sandbox_id")
                and row.get("status") in ACTIVE_SANDBOX_STATUSES | {"provisioning"}
            ),
        )
        decision = release_decision(
            row=row,
            outcome=outcome,
            active_experiment_ids=self._active_experiment_ids_for_row(row=row),
        )
        self.lifecycle.apply(row=row, decision=decision)
        if outcome == "maybe_alive":
            view = self._row_view(row=self.repository.get_by_uid(sandbox_uid=sandbox_uid))
            view["hint"] = (
                "Release did NOT complete: the provider terminate call failed and the VM may still be running (and billing). The sandbox stays active; retry sandbox.release, or the expiry reaper will retry at the deadline."
            )
            return view
        view = self._row_view(row=self.repository.get_by_uid(sandbox_uid=sandbox_uid))
        if was_active:
            view["hint"] = (
                "Sandbox terminated. The VM and files on it are gone. Only files the agent explicitly copied or uploaded before release remain durable."
            )
        else:
            view["hint"] = "Sandbox terminated. No running sandbox needed teardown."
        return view

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

    def pull_outputs_command(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a filled rsync command a non-local (key) caller runs itself.

        The control plane never touches local files or runs rsync (no-dataplane
        Phase C): a cloud agent has no local checkout, so it copies outputs off
        the box with its OWN SSH key. host/port/user/remote-path are filled from
        the project-scoped sandbox facts; the caller fills <key_path> and
        <local-destination>. Bytes go agent<->box directly, never through the
        brain. This is the key-principal counterpart to the proxy's local
        pull_outputs; the local data-plane path is unchanged."""
        facts = self.get(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
            include_data_plane_enrichment=False,
        )
        ssh = facts.get("ssh") or {}
        host = str(ssh.get("host") or "")
        port = int(ssh.get("port") or 0) or 22
        user = str(ssh.get("user") or "")
        remote_dir = str(facts.get("experiment_dir") or "").rstrip("/")
        if facts.get("status") != "running" or not host or not remote_dir:
            return {
                "sandbox_uid": facts.get("sandbox_uid"),
                "experiment_id": facts.get("experiment_id"),
                "project_id": facts.get("project_id"),
                "status": facts.get("status"),
                "rsync": "",
                "hint": (
                    "No running sandbox to pull from. Provision or wait for the "
                    "sandbox to reach status 'running', then re-call."
                ),
            }
        wanted = list(paths) if paths else list(_DEFAULT_PULL_OUTPUTS)
        remote_sources = _pull_output_sources(
            remote_dir=remote_dir,
            user=user,
            host=host,
            paths=wanted,
        )
        rsync = _RSYNC_PULL_OUTPUTS_TEMPLATE.format(
            port=port, remote_sources=" ".join(remote_sources)
        )
        view = {
            "sandbox_uid": facts.get("sandbox_uid"),
            "experiment_id": facts.get("experiment_id"),
            "project_id": facts.get("project_id"),
            "status": "running",
            "experiment_dir": remote_dir,
            "paths": wanted,
            "rsync": rsync,
            "hint": (
                "Run this rsync locally with your own private key: replace "
                "<key_path> with the path to the private key whose public key "
                "you authorized on this sandbox, and <local-destination> with a "
                "local directory. The brain does not run rsync or hold your key; "
                "bytes move directly between your machine and the box. Pull what "
                "you need before sandbox.release."
            ),
        }
        return self._with_runs_nudge(
            view=view, sandbox_uid=str(facts.get("sandbox_uid") or "")
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
