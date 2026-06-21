"""Ports for sandbox sync session boundaries."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict


class RunningSandboxSyncRow(TypedDict):
    """Provider-portable sandbox facts needed to mint a sync session."""

    experiment_id: str
    tenant_id: str | None
    sandbox_id: str | None
    ssh_host: str | None
    ssh_port: int | None
    ssh_user: str | None
    sync_dir: str | None
    workdir: str | None
    sandbox_data_dir: str | None
    unsynced_dir: str | None


class SyncTarget(TypedDict):
    """A running sandbox row with a freshly granted sync session."""

    row: RunningSandboxSyncRow
    session: dict[str, Any]


class ControlPlaneView(Protocol):
    """Lease-backed sandbox targets visible to a sync poller."""

    def sync_targets(self, *, tenant_id: str | None = None) -> list[SyncTarget]:
        ...


class RunningSandboxRows(Protocol):
    """Provides provider-portable rows for running sandboxes."""

    def list_running_sync_rows(self) -> list[RunningSandboxSyncRow]:
        ...


class SyncSessionIssuer(Protocol):
    """Issues sync sessions for provisioned sandboxes."""

    def grant(
        self,
        *,
        experiment_id: str,
        sandbox_id: str,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        experiment_dir: str,
        data_dir: str = "",
    ) -> dict[str, Any]:
        ...
