"""Ports for sandbox lifecycle collaborators."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


DEFAULT_STALE_PROVISION_DEADLINE_SECONDS = 10 * 60.0


class ProvisionReaper(Protocol):
    """The one provisioner duty the daemons' reaper loop schedules."""

    def reap_stale_provisions(
        self, *, now: datetime, deadline_seconds: float
    ) -> int:
        ...


class SandboxMaintenance(ProvisionReaper, Protocol):
    """Sandbox operations used by cross-component housekeeping."""

    def reconcile_running_rows(self) -> int: ...
