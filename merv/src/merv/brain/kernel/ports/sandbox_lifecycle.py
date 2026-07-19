"""Ports for sandbox lifecycle collaborators."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ProvisionReaper(Protocol):
    """The one provisioner duty the daemons' reaper loop schedules."""

    def reap_stale_provisions(
        self, *, now: datetime, deadline_seconds: float
    ) -> int:
        ...
