"""Chaos scenarios for the split topology (cloud plan Phase 9).

No docker — the fakes stand in for the provider and the daemon. Each scenario
exercises a failure the split design must survive:

- daemon dies mid-provision  ⇒ the stale-awaiting_initial_push reap (Step 2)
  terminates the billing sandbox so a dead daemon never leaves a VM running with
  no files (risk 8).
- daemon dies mid-sync        ⇒ the lease-expiry sweep releases the abandoned
  lease so another client can take the experiment over (risk on multi-client
  coordination).
- control restart             ⇒ the crash-recovery scan (Phase 8) resumes the
  reaper, and the cleanup sweeps then reconcile/terminate as expected (risk 6).
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
from backend.services.sync_sessions import LeaseService
from backend.utils import PermissionDeniedError
from tests.fakes import FakeRsyncSyncer


class _Base(unittest.TestCase):
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
        self.backend.capabilities = BackendCapabilities(name="fake")
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.store = self.app.store
        self.cleanup = CleanupService(sandboxes=self.app.sandboxes, blobs=self.app.blobs)
        self.project_id = self.app.call_tool("project.create", {"name": "Chaos"})["id"]

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


class DaemonDiesMidProvisionTest(_Base):
    def test_orphan_cleanup_terminates_the_billing_sandbox(self) -> None:
        # A VM was created (billing) but the initial push never completed because
        # the daemon died — the row is wedged in awaiting_initial_push.
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=self.project_id,
            sandbox_id="sb-billing",
            status="provisioning",
            phase="awaiting_initial_push",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        self.backend.alive["sb-billing"] = True
        self.backend.by_experiment[exp_id] = "sb-billing"

        # 30 minutes pass with no daemon to finish the push.
        now = datetime(2026, 1, 1, 0, 30, 0, tzinfo=UTC)
        report = self.cleanup.run_all(now=now)

        self.assertEqual(report.stale_provisions_reaped, 1)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "failed")
        # The billing VM was terminated — no forever-billing orphan.
        self.assertIn("sb-billing", self.backend.terminated)
        self.assertFalse(self.backend.is_alive(sandbox_id="sb-billing"))


class DaemonDiesMidSyncTest(_Base):
    def test_lease_expiry_lets_another_client_take_over(self) -> None:
        leases: LeaseService = self.app.sandboxes.leases
        # Client A holds the sync lease, then dies mid-sync (never releases).
        a = leases.acquire(
            experiment_id="exp_sync", holder_client_id="client-A", ttl_seconds=120
        )
        self.assertEqual(a["holder_client_id"], "client-A")
        # While A's lease is live, B cannot take over.
        with self.assertRaises(PermissionDeniedError):
            leases.acquire(
                experiment_id="exp_sync", holder_client_id="client-B", ttl_seconds=120
            )

        # Time passes past A's TTL with A gone; the cleanup sweep releases it.
        future = datetime.now(tz=UTC) + timedelta(hours=1)
        released = self.cleanup.sweep_expired_leases(now=future)
        self.assertEqual(released, 1)

        # Now B takes the experiment over with a fresh lease id (A's reports
        # would be rejected as stale).
        b = leases.acquire(
            experiment_id="exp_sync", holder_client_id="client-B", ttl_seconds=120
        )
        self.assertEqual(b["holder_client_id"], "client-B")
        self.assertNotEqual(b["id"], a["id"])

    def test_release_surfaces_daemon_unreachable(self) -> None:
        # Release with a final-pull that fails (daemon unreachable) still frees
        # billing AND flags the unreachable state for the UI.
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=self.project_id,
            sandbox_id="sb-x",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            expires_at="2999-01-01T00:00:00Z",
        )
        self.backend.alive["sb-x"] = True
        original = self.app.sandboxes._final_pull_row

        def _boom(*, row):  # noqa: ANN001
            raise RuntimeError("daemon unreachable")

        self.app.sandboxes._final_pull_row = _boom
        try:
            view = self.app.sandboxes.release(
                experiment_id=exp_id, project_id=self.project_id
            )
        finally:
            self.app.sandboxes._final_pull_row = original
        self.assertTrue(view["daemon_unreachable"])
        # Billing still freed: the VM was terminated despite the failed pull.
        self.assertIn("sb-x", self.backend.terminated)


class ControlRestartTest(unittest.TestCase):
    """A control restart resumes reaping; cleanup then reconciles (risk 6)."""

    _ENV = {
        "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL": "3600",
        "RESEARCH_PLUGIN_SANDBOX_RSYNC_INTERVAL": "3600",
        "RESEARCH_PLUGIN_MODE": "control",
        "RESEARCH_PLUGIN_TASK_RESULT_TIMEOUT": "2",
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in self._ENV}
        os.environ.update(self._ENV)
        self._apps: list = []

    def tearDown(self) -> None:
        for app in self._apps:
            app.shutdown()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _reaper_backend(self) -> FakeSandboxBackend:
        backend = FakeSandboxBackend()
        backend.capabilities = BackendCapabilities(
            name="fake", enforce_expiry=True, auto_sync=True
        )
        return backend

    def _build(self):
        from backend.composition.control_mode import build_control_app

        backend = self._reaper_backend()
        app, _queue, _auth = build_control_app(
            repo_root=self.staging, execution_backend=backend
        )
        self._apps.append(app)
        return app, backend

    def test_restart_resumes_then_cleanup_reaps_dead_vm(self) -> None:
        first, _ = self._build()
        project_id = first.call_tool("project.create", {"name": "Cloud"})["id"]
        exp_id = first.call_tool(
            "experiment.create",
            {"project_id": project_id, "name": "x", "intent": "y"},
        )["id"]
        first.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=project_id,
            sandbox_id="sb-dead",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            expires_at="2999-01-01T00:00:00Z",
        )
        first.shutdown()
        self._apps.remove(first)

        # Restart the control app over the SAME staging dir (same SQLite store).
        # The VM is NOT marked alive in the fresh backend, so the provider says
        # it's gone. The restart's crash-recovery scan (Phase 8) reconciles the
        # running rows and resumes the reaper; the orphan-cleanup sweep is the
        # belt-and-suspenders that finishes the job. Either way the dead VM's row
        # must end up terminated — a control restart orphans nothing (risk 6).
        restarted, _backend = self._build()
        cleanup = CleanupService(sandboxes=restarted.sandboxes, blobs=restarted.blobs)
        reaped = cleanup.sweep_orphan_vms()
        row = restarted.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(
            row["status"],
            "terminated",
            "a dead VM's row must be terminated after a control restart",
        )
        # The sweep is idempotent with crash recovery: whether recovery already
        # reaped it (reaped == 0) or the sweep did (reaped == 1), the end state
        # is the same and a second pass changes nothing.
        self.assertIn(reaped, (0, 1))
        self.assertEqual(cleanup.sweep_orphan_vms(), 0)


if __name__ == "__main__":
    unittest.main()
