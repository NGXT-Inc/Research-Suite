"""Ports for sandbox sync session boundaries."""

from __future__ import annotations

from typing import Any, Protocol


class ControlPlaneView(Protocol):
    """Lease-backed sandbox targets visible to a sync poller."""

    def sync_targets(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
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
