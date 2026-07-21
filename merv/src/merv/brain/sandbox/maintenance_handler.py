"""Operational Sandbox maintenance delegated by the public facade."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .handler import SandboxHandler


class SandboxMaintenanceHandler(SandboxHandler):
    def reap_expired(self, **kwargs: Any) -> int:
        return self.lifecycle.reap_expired(**kwargs)

    def reap_idle(self, **kwargs: Any) -> int:
        return self.daemons.reap_idle(**kwargs)

    def reconcile_running_rows(self) -> int:
        left_running = 0
        for row in self.registry.list_running_rows():
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


__all__ = ["SandboxMaintenanceHandler"]
