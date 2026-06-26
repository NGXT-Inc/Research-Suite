"""Idempotent cloud cleanup sweeps (cloud plan Phase 9).

The control plane needs periodic housekeeping that the local-mode in-process
daemons never had to do at scale: terminate orphaned billing VMs, garbage-
collect expired blobs, and reap provisions that wedged mid-provision.
Heavy-storage expiry joins the same pattern. These are **pure,
clock-injectable functions** —
each takes ``now`` and returns a count — grouped behind ``CleanupService`` with
a single ``run_all(now=...)`` entry point.

Deliberately NOT a scheduler. There is no thread, no cron daemon, no timer here:
scheduling is a documented seam. ``run_all`` is callable from a future cloud
scheduler (a managed cron, a sidecar tick, a `/admin/cleanup` endpoint), and the
control composition exposes the built service so an operator or a test can drive
one pass. This keeps the sweeps unit-testable with injected clocks and keeps the
control plane free of a long-lived scheduler we are not ready to own (the
reaper thread, which IS owned, stays in SandboxDaemons).

Every sweep is idempotent and best-effort per item: one bad row never aborts the
pass. The sweeps reuse the existing primitives — ``provisioner.reconcile`` /
``cleanup_orphan`` for VMs and ``blobs.sweep_expired`` for blobs — rather than
re-deriving termination logic, so the reaper and the sweeps can never disagree
about what "gone" means.

Local mode never runs these (it has no cloud cron); they are control-plane work.
But they are mode-blind — the same SandboxService + blob store — so the
in-process tests exercise the exact code the control plane schedules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..sandbox.sandbox_support import DEFAULT_STALE_PROVISION_DEADLINE_SECONDS
from ..utils import format_iso


@dataclass(frozen=True)
class CleanupReport:
    """Per-sweep counts from one ``run_all`` pass (for logs/metrics/tests)."""

    orphan_vms_reaped: int = 0
    blobs_swept: int = 0
    storage_objects_swept: int = 0
    stale_provisions_reaped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "orphan_vms_reaped": self.orphan_vms_reaped,
            "blobs_swept": self.blobs_swept,
            "storage_objects_swept": self.storage_objects_swept,
            "stale_provisions_reaped": self.stale_provisions_reaped,
        }


class CleanupService:
    """The control plane's cleanup sweeps, grouped behind ``run_all``.

    Constructed from a ``SandboxService`` (registry + provisioner + backend),
    a ``BlobStore``, and optionally storage. Every method takes ``now``
    so a scheduler or a test drives the clock.
    """

    def __init__(
        self,
        *,
        sandboxes: Any,
        blobs: Any,
        storage: Any | None = None,
        stale_provision_deadline_seconds: float = DEFAULT_STALE_PROVISION_DEADLINE_SECONDS,
    ) -> None:
        self.sandboxes = sandboxes
        self.blobs = blobs
        self.storage = storage
        self.stale_provision_deadline_seconds = float(stale_provision_deadline_seconds)

    # ---------- the entry point a scheduler calls ----------

    def run_all(self, *, now: datetime | None = None) -> CleanupReport:
        now_dt = now or datetime.now(tz=UTC)
        return CleanupReport(
            orphan_vms_reaped=self.sweep_orphan_vms(now=now_dt),
            blobs_swept=self.sweep_expired_blobs(now=now_dt),
            storage_objects_swept=self.sweep_expired_storage(now=now_dt),
            stale_provisions_reaped=self.sweep_stale_provisions(now=now_dt),
        )

    # ---------- individual sweeps (each idempotent, clock-injectable) ----------

    def sweep_orphan_vms(self, *, now: datetime | None = None) -> int:
        """Reconcile every running row against the provider.

        A row whose backend sandbox is gone (``is_alive`` false) is marked
        terminated by ``reconcile``; the inverse — a provider VM with no running
        row — is covered by ``cleanup_orphan`` (deterministic-name lookup) on
        the next provision and by reconcile marking the row terminated, so a
        ghost row never keeps billing. Best-effort per row.
        """
        reaped = 0
        for row in self.sandboxes.registry.list_running_rows():
            before = row.get("status")
            try:
                fresh = self.sandboxes.provisioner.reconcile(row=row)
            except Exception:  # noqa: BLE001 — one bad row never aborts the pass
                continue
            if before == "running" and (fresh or {}).get("status") != "running":
                reaped += 1
        return reaped

    def sweep_expired_blobs(self, *, now: datetime | None = None) -> int:
        """Delete blobs past their TTL across all tenants (blob TTL GC)."""
        now_iso = format_iso(now or datetime.now(tz=UTC))
        try:
            return int(self.blobs.sweep_expired(now=now_iso))
        except Exception:  # noqa: BLE001 — a GC failure must not abort the pass
            return 0

    def sweep_expired_storage(self, *, now: datetime | None = None) -> int:
        """Expire heavy storage rows through the ledger (refcount-aware GC)."""
        if self.storage is None:
            return 0
        now_iso = format_iso(now or datetime.now(tz=UTC))
        try:
            return int(self.storage.sweep_expired(now=now_iso))
        except Exception:  # noqa: BLE001
            return 0

    def sweep_stale_provisions(self, *, now: datetime | None = None) -> int:
        """Reap rows wedged in ANY pre-running provisioning phase past the deadline.

        Risk 8 (daemon offline mid-provision → billing VM): a provision can wedge
        in any pre-running phase — ``creating`` / ``connecting`` — and the
        provider VM already exists from ``creating`` onward, so the reap must
        not be phase-specific.
        Delegates to the shared ``provisioner.reap_stale_provisions`` so this
        sweep and the always-running reaper thread can never disagree about what
        'wedged' means. Idempotent — a row that already settled is skipped.
        """
        now_dt = now or datetime.now(tz=UTC)
        return self.sandboxes.provisioner.reap_stale_provisions(
            now=now_dt, deadline_seconds=self.stale_provision_deadline_seconds
        )
