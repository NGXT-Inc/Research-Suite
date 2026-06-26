"""Chaos scenarios for the split topology (cloud plan Phase 9).

No docker — the fakes stand in for the provider and the daemon. Each scenario
exercises a failure the split design must survive:

- daemon dies mid-provision  ⇒ the stale-provision reap (Step 2)
  terminates the billing sandbox so a dead daemon never leaves a VM running with
  no owner (risk 8).
- control restart             ⇒ the crash-recovery scan (Phase 8) resumes the
  reaper, and the cleanup sweeps then reconcile/terminate as expected (risk 6).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.config import MGMT_KEY_PATH_ENV_VAR, MGMT_PUBLIC_KEY_ENV_VAR
from backend.execution.backends.fake import FakeSandboxBackend
from backend.sandbox.sandbox_backend import BackendCapabilities
from backend.services.cleanup import CleanupService


def _mounted_mgmt_key_env(root: Path) -> dict[str, str]:
    key_path = root / "managed_key"
    key_path.write_text("PRIVATE KEY\n", encoding="utf-8")
    key_path.chmod(0o600)
    return {
        MGMT_KEY_PATH_ENV_VAR: str(key_path),
        MGMT_PUBLIC_KEY_ENV_VAR: "ssh-ed25519 AAAAmanaged",
    }


class _Base(unittest.TestCase):
    _ENV = {
        "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL": "3600",
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
        # A VM was created (billing) but provisioning never completed because
        # the daemon died — the row is wedged before running.
        exp_id = self._experiment()
        sandbox_uid = "uid_billing"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            project_id=self.project_id,
            sandbox_id="sb-billing",
            status="provisioning",
            phase="connecting",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        self.backend.alive["sb-billing"] = True
        self.backend.by_experiment[exp_id] = "sb-billing"

        # 30 minutes pass with no daemon to finish the push.
        now = datetime(2026, 1, 1, 0, 30, 0, tzinfo=UTC)
        report = self.cleanup.run_all(now=now)

        self.assertEqual(report.stale_provisions_reaped, 1)
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=sandbox_uid)
        self.assertEqual(row["status"], "failed")
        # The billing VM was terminated — no forever-billing orphan.
        self.assertIn("sb-billing", self.backend.terminated)
        self.assertFalse(self.backend.is_alive(sandbox_id="sb-billing"))


class ControlRestartTest(unittest.TestCase):
    """A control restart resumes reaping; cleanup then reconciles (risk 6)."""

    _ENV = {
        "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL": "3600",
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
            name="fake", enforce_expiry=True
        )
        return backend

    def _build(self):
        from backend.composition.control_mode import build_control_app

        backend = self._reaper_backend()
        app, _queue = build_control_app(
            repo_root=self.staging,
            env=_mounted_mgmt_key_env(self.staging),
            execution_backend=backend,
        )
        self._apps.append(app)
        return app, backend

    def test_restart_resumes_then_cleanup_reaps_dead_vm(self) -> None:
        first, _ = self._build()
        project_id = first.call_tool("project.create", {"name": "Cloud"})["id"]
        exp_id = first.call_tool(
            "experiment.create",
            {"project_id": project_id, "name": "exp-x", "intent": "y"},
        )["id"]
        sandbox_uid = "uid_dead_restart"
        first.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
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
        row = restarted.sandboxes.registry.get_by_uid(sandbox_uid=sandbox_uid)
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
