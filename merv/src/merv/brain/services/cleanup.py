"""Idempotent brain housekeeping sweeps.

``CleanupService`` reconciles tracked running sandbox rows, garbage-collects
expired submitted blobs and heavy-storage aliases, and reaps stale provisioning
rows. It does not discover arbitrary provider VMs without a registry row.

This module is deliberately not a scheduler. Operators invoke ``run_all``
through the private admin endpoint from managed cron or a sidecar; the separate
sandbox expiry reaper remains owned by ``SandboxService``.

Every sweep is idempotent and best-effort per item: one bad row never aborts the
pass. The sweeps reuse the existing primitives — the sandbox service's
``reconcile_running_rows`` / ``reap_stale_provisions`` (both backed by
`SandboxLifecycle`) for VMs and ``blobs.sweep_expired`` for blobs — rather than
re-deriving termination logic, so the reaper and the sweeps can never disagree
about what "gone" means.

The sweeps are adapter-neutral and can be exercised with either deployment
preset, though only an operator-scheduled hosted deployment normally runs them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..sandbox.sandbox_support import DEFAULT_STALE_PROVISION_DEADLINE_SECONDS
from ..kernel.utils import format_iso


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
    """The brain's cleanup sweeps, grouped behind ``run_all``.

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
        return self.sandboxes.reconcile_running_rows()

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
        return self.sandboxes.reap_stale_provisions(
            now=now_dt, deadline_seconds=self.stale_provision_deadline_seconds
        )
