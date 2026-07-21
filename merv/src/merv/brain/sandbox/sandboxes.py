"""Stable Sandbox facade over typed command, query, and maintenance handlers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ..kernel.env import env_float
from ..kernel.ports.quota_admission import QuotaAdmission
from ..kernel.ports.sandbox_worker import SandboxWorker
from ..kernel.utils import ValidationError
from .commands import SandboxCommandHandler
from .maintenance_handler import SandboxMaintenanceHandler
from .messages import (
    AttachSandboxCommand,
    ExtendSandboxCommand,
    GetSandboxQuery,
    ListSandboxesQuery,
    ReleaseSandboxCommand,
    RequestSandboxCommand,
    SandboxOptionsQuery,
    SandboxRunsQuery,
    SandboxTerminalQuery,
)
from .queries import SandboxQueryHandler
from .runtime import SandboxRuntime
from .sandbox_heartbeat import SandboxActivityPolicy
from .sandbox_support import DEFAULT_REQUEST_WAIT_SECONDS, RUNS_WAIT_POLL_SECONDS


def _message(message_type: type[Any], values: dict[str, Any]) -> Any:
    """Build a typed boundary value from a public method's named arguments."""
    return message_type(
        **{name: value for name, value in values.items() if name != "self"}
    )


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
        self.registry = self.repository
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
        self.maintenance = SandboxMaintenanceHandler(self)

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
        return self.commands.execute_request(_message(RequestSandboxCommand, locals()))

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
        return self.queries.execute_get(_message(GetSandboxQuery, locals()))

    def attach(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        sandbox_uid: str,
        include_data_plane_enrichment: bool = True,
        public_key_override: str | None = None,
    ) -> dict[str, Any]:
        return self.commands.execute_attach(_message(AttachSandboxCommand, locals()))

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
        return self.commands.execute_extend(_message(ExtendSandboxCommand, locals()))

    def options(
        self,
        *,
        project_id: str | None = None,
        gpu: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        return self.queries.execute_options(_message(SandboxOptionsQuery, locals()))

    def list_sandboxes(self, *, project_id: str | None = None) -> dict[str, Any]:
        return self.queries.execute_list(_message(ListSandboxesQuery, locals()))

    def release(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
        confirm_retained: bool = False,
    ) -> dict[str, Any]:
        return self.commands.execute_release(_message(ReleaseSandboxCommand, locals()))

    def terminal(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
        tail: int | None = None,
        since: int | None = None,
    ) -> dict[str, Any]:
        return self.queries.execute_terminal(_message(SandboxTerminalQuery, locals()))

    def runs(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        return self.queries.execute_runs(_message(SandboxRunsQuery, locals()))

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

    def sandboxes_for_experiment(
        self, *, conn, experiment_id: str
    ) -> list[dict[str, Any]]:
        return self.queries.sandboxes_for_experiment(
            conn=conn, experiment_id=experiment_id
        )

    def sandboxes_for_project(self, *, conn, project_id: str) -> list[dict[str, Any]]:
        return self.queries.sandboxes_for_project(conn=conn, project_id=project_id)

    def reap_expired(self, **kwargs: Any) -> int:
        return self.maintenance.reap_expired(**kwargs)

    def reap_idle(self, **kwargs: Any) -> int:
        return self.maintenance.reap_idle(**kwargs)

    def reconcile_running_rows(self) -> int:
        return self.maintenance.reconcile_running_rows()

    def reap_stale_provisions(self, *, now: datetime, deadline_seconds: float) -> int:
        return self.maintenance.reap_stale_provisions(
            now=now, deadline_seconds=deadline_seconds
        )


SandboxService = SandboxFacade

__all__ = ["SandboxFacade", "SandboxService"]
