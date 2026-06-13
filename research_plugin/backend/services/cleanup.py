"""Idempotent cloud cleanup sweeps (cloud plan Phase 9).

The control plane needs periodic housekeeping that the local-mode in-process
daemons never had to do at scale: terminate orphaned billing VMs, garbage-
collect expired blobs, release dead sync leases, and reap provisions that wedged
mid-push. Phase 9 implements these as **pure, clock-injectable functions** —
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
``cleanup_orphan`` for VMs, ``blobs.sweep_expired`` for blobs,
``LeaseService.sweep_expired`` for leases — rather than re-deriving termination
logic, so the reaper and the sweeps can never disagree about what "gone" means.

Local mode never runs these (it has no cloud cron); they are control-plane work.
But they are mode-blind — the same SandboxService + blob store + leases — so the
in-process tests exercise the exact code the control plane schedules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .sandbox_support import parse_iso


# How long a row may sit in the explicit ``awaiting_initial_push`` phase before
# the stale-provision sweep declares the daemon dead and reaps it. The provider
# VM exists and is billing by this point (it was created before the push), so
# this is the billing-protection deadline for risk 8 (daemon offline
# mid-provision). Comfortably above a slow first rsync; well below an hour.
DEFAULT_AWAITING_PUSH_DEADLINE_SECONDS = 10 * 60.0


@dataclass(frozen=True)
class CleanupReport:
    """Per-sweep counts from one ``run_all`` pass (for logs/metrics/tests)."""

    orphan_vms_reaped: int = 0
    blobs_swept: int = 0
    leases_released: int = 0
    stale_provisions_reaped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "orphan_vms_reaped": self.orphan_vms_reaped,
            "blobs_swept": self.blobs_swept,
            "leases_released": self.leases_released,
            "stale_provisions_reaped": self.stale_provisions_reaped,
        }


class CleanupService:
    """The control plane's cleanup sweeps, grouped behind ``run_all``.

    Constructed from a ``SandboxService`` (registry + provisioner + backend +
    leases) and a ``BlobStore``. Every method takes ``now`` so a scheduler or a
    test drives the clock; ``run_all`` runs all four and returns a CleanupReport.
    """

    def __init__(
        self,
        *,
        sandboxes: Any,
        blobs: Any,
        awaiting_push_deadline_seconds: float = DEFAULT_AWAITING_PUSH_DEADLINE_SECONDS,
    ) -> None:
        self.sandboxes = sandboxes
        self.blobs = blobs
        self.awaiting_push_deadline_seconds = float(awaiting_push_deadline_seconds)

    # ---------- the entry point a scheduler calls ----------

    def run_all(self, *, now: datetime | None = None) -> CleanupReport:
        now_dt = now or datetime.now(tz=UTC)
        return CleanupReport(
            orphan_vms_reaped=self.sweep_orphan_vms(now=now_dt),
            blobs_swept=self.sweep_expired_blobs(now=now_dt),
            leases_released=self.sweep_expired_leases(now=now_dt),
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
        now_iso = (now or datetime.now(tz=UTC)).isoformat()
        try:
            return int(self.blobs.sweep_expired(now=now_iso))
        except Exception:  # noqa: BLE001 — a GC failure must not abort the pass
            return 0

    def sweep_expired_leases(self, *, now: datetime | None = None) -> int:
        """Release every sync lease past its expiry (lease-expiry sweep)."""
        try:
            return int(self.sandboxes.leases.sweep_expired(now=now))
        except Exception:  # noqa: BLE001
            return 0

    def sweep_stale_provisions(self, *, now: datetime | None = None) -> int:
        """Reap rows stuck in ``awaiting_initial_push`` past the deadline.

        Risk 8 (daemon offline mid-provision → billing VM, no files): the VM was
        created, but the initial push never confirmed, so the row sits in
        ``provisioning`` / phase ``awaiting_initial_push``. After the deadline,
        terminate any orphan VM and mark the row failed so it stops being a
        billing ghost and the agent can request a fresh sandbox. Idempotent —
        a row that already settled is skipped.
        """
        now_dt = now or datetime.now(tz=UTC)
        reaped = 0
        for row in self.sandboxes.registry.list_rows_by_status(status="provisioning"):
            if str(row.get("phase") or "") != "awaiting_initial_push":
                continue
            started = parse_iso(row.get("provision_started_at"))
            if started is None:
                continue
            age = (now_dt - started).total_seconds()
            if age < self.awaiting_push_deadline_seconds:
                continue
            experiment_id = str(row.get("experiment_id") or "")
            try:
                self.sandboxes.provisioner.cleanup_orphan(
                    experiment_id=experiment_id, row=row
                )
                self.sandboxes.registry.mark_failed(
                    experiment_id=experiment_id,
                    error=(
                        "initial push never completed (daemon offline?); "
                        "the sandbox was terminated — call sandbox.request again"
                    ),
                )
                self.sandboxes.registry.emit_event(
                    project_id=str(row.get("project_id") or ""),
                    event_type="sandbox.failed",
                    experiment_id=experiment_id,
                    payload={
                        "error": "awaiting_initial_push stale-reaped",
                        "sandbox_id": row.get("sandbox_id", ""),
                    },
                )
                reaped += 1
            except Exception:  # noqa: BLE001 — best-effort per row
                continue
        return reaped
