"""Cloud cleanup sweeps (cloud plan Phase 9), driven by injected clocks.

The four idempotent sweeps grouped behind CleanupService.run_all — orphan-VM,
blob TTL GC, lease-expiry, and stale-awaiting_initial_push reap — each take a
``now`` so the test owns the clock. The service is mode-blind (the in-process
app exercises the exact code the control plane schedules), so these run without
docker or a real control plane.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.execution.types import BackendCapabilities
from backend.services.cleanup import CleanupService
from tests.fakes import FakeRsyncSyncer


class CleanupSweepTest(unittest.TestCase):
    # Park the background loops so the test, not a timer, drives every sweep.
    _ENV = {
        "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL": "3600",
        "RESEARCH_PLUGIN_SANDBOX_RSYNC_INTERVAL": "3600",
        "RESEARCH_PLUGIN_SANDBOX_AUTO_RSYNC": "0",
        "RESEARCH_PLUGIN_SANDBOX_REAPER": "0",
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in self._ENV}
        os.environ.update(self._ENV)
        self.backend = FakeSandboxBackend()
        # enforce_expiry off keeps the reaper inert; the sweeps drive themselves.
        self.backend.capabilities = BackendCapabilities(name="fake")
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.store = self.app.store
        self.cleanup = CleanupService(
            sandboxes=self.app.sandboxes, blobs=self.app.blobs
        )
        self.project_id = self.app.call_tool("project.create", {"name": "C"})["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _experiment(self) -> str:
        return self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": "exp", "intent": "x"},
        )["id"]

    # ---- orphan-VM sweep ----

    def test_orphan_vm_sweep_reaps_a_running_row_whose_vm_is_gone(self) -> None:
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=self.project_id,
            sandbox_id="sb-gone",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            expires_at="2999-01-01T00:00:00Z",
        )
        # The provider says the VM is gone (never marked alive in the fake).
        self.assertFalse(self.backend.is_alive(sandbox_id="sb-gone"))
        reaped = self.cleanup.sweep_orphan_vms(now=datetime.now(tz=UTC))
        self.assertEqual(reaped, 1)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "terminated")

    def test_orphan_vm_sweep_leaves_a_live_row_running(self) -> None:
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=self.project_id,
            sandbox_id="sb-live",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            expires_at="2999-01-01T00:00:00Z",
        )
        self.backend.alive["sb-live"] = True
        reaped = self.cleanup.sweep_orphan_vms(now=datetime.now(tz=UTC))
        self.assertEqual(reaped, 0)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "running")

    # ---- blob TTL GC ----

    def test_blob_ttl_gc_deletes_expired_blobs(self) -> None:
        ns = self.project_id
        live = self.app.blobs.put(
            namespace=ns, data=b"keep", expires_at="2999-01-01T00:00:00Z"
        )
        dead = self.app.blobs.put(
            namespace=ns, data=b"drop", expires_at="2000-01-01T00:00:00Z"
        )
        swept = self.cleanup.sweep_expired_blobs(now=datetime.now(tz=UTC))
        self.assertEqual(swept, 1)
        self.assertIsNotNone(self.app.blobs.stat(namespace=ns, sha256=live))
        self.assertIsNone(self.app.blobs.stat(namespace=ns, sha256=dead))

    # ---- lease-expiry sweep ----

    def test_lease_expiry_sweep_releases_expired_leases(self) -> None:
        leases = self.app.sandboxes.leases
        # A short-TTL lease, then sweep at a clock well past its expiry.
        leases.acquire(
            experiment_id="exp_lease", holder_client_id="c1", ttl_seconds=1
        )
        self.assertIsNotNone(leases.holder(experiment_id="exp_lease"))
        future = datetime.now(tz=UTC) + timedelta(hours=1)
        released = self.cleanup.sweep_expired_leases(now=future)
        self.assertEqual(released, 1)
        self.assertIsNone(leases.holder(experiment_id="exp_lease"))

    def test_lease_expiry_sweep_keeps_a_live_lease(self) -> None:
        leases = self.app.sandboxes.leases
        leases.acquire(
            experiment_id="exp_live", holder_client_id="c1", ttl_seconds=600
        )
        released = self.cleanup.sweep_expired_leases(now=datetime.now(tz=UTC))
        self.assertEqual(released, 0)
        self.assertIsNotNone(leases.holder(experiment_id="exp_live"))

    # ---- stale awaiting_initial_push reap ----

    def test_stale_awaiting_push_reaped_past_deadline(self) -> None:
        exp_id = self._experiment()
        started = "2026-01-01T00:00:00Z"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=self.project_id,
            sandbox_id="sb-wedged",
            status="provisioning",
            phase="awaiting_initial_push",
            provision_started_at=started,
        )
        self.backend.alive["sb-wedged"] = True
        self.backend.by_experiment[exp_id] = "sb-wedged"
        # 20 minutes later, well past the 10-minute awaiting-push deadline.
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        reaped = self.cleanup.sweep_stale_provisions(now=now)
        self.assertEqual(reaped, 1)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "failed")
        # The billing VM was terminated by cleanup_orphan.
        self.assertIn("sb-wedged", self.backend.terminated)

    def test_stale_awaiting_push_left_alone_within_deadline(self) -> None:
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=self.project_id,
            sandbox_id="sb-fresh",
            status="provisioning",
            phase="awaiting_initial_push",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        # Only 2 minutes in — under the deadline, so it keeps provisioning.
        now = datetime(2026, 1, 1, 0, 2, 0, tzinfo=UTC)
        reaped = self.cleanup.sweep_stale_provisions(now=now)
        self.assertEqual(reaped, 0)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "provisioning")

    # ---- run_all ----

    def test_run_all_returns_per_sweep_counts_and_is_idempotent(self) -> None:
        # One expired blob + one dead-VM row + one expired lease.
        self.app.blobs.put(
            namespace=self.project_id, data=b"x", expires_at="2000-01-01T00:00:00Z"
        )
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=self.project_id,
            sandbox_id="sb-dead",
            status="running",
            expires_at="2999-01-01T00:00:00Z",
        )
        self.app.sandboxes.leases.acquire(
            experiment_id="exp_l", holder_client_id="c", ttl_seconds=1
        )
        future = datetime.now(tz=UTC) + timedelta(hours=1)
        report = self.cleanup.run_all(now=future)
        self.assertEqual(report.orphan_vms_reaped, 1)
        self.assertEqual(report.blobs_swept, 1)
        self.assertEqual(report.leases_released, 1)
        # A second pass over the cleaned state changes nothing.
        report2 = self.cleanup.run_all(now=future)
        self.assertEqual(report2.as_dict(), {
            "orphan_vms_reaped": 0,
            "blobs_swept": 0,
            "leases_released": 0,
            "stale_provisions_reaped": 0,
        })


if __name__ == "__main__":
    unittest.main()
