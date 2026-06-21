"""Daemon-restart coverage for the shared-mode ProjectRouter.

The expiration reaper lives inside each project's SandboxService, but in
shared multi-project mode the router builds apps lazily on first tool call.
These tests pin the startup behavior that keeps an expired Lambda VM from
billing forever after a restart: any registered project whose state DB has a
running/provisioning sandbox gets its app (and therefore its reaper) eagerly
on router construction, and reaping reverts the experiment to ready_to_run.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.execution.backends.fake import FakeSandboxBackend
from backend.sandbox_backend import BackendCapabilities
from backend.project_router import ProjectRouter
from tests.fakes import FakeRsyncSyncer


def _reaper_capable_backend_factory(_repo: Path) -> FakeSandboxBackend:
    """A fake backend with remote-sandbox daemon behavior enabled."""
    backend = FakeSandboxBackend()
    backend.capabilities = BackendCapabilities(
        name="fake",
        enforce_expiry=True,
        auto_sync=True,
    )
    return backend


class RouterRestartReaperTest(unittest.TestCase):
    # Keep the background loops alive but inert: the first reap/sync only
    # happens after one full interval, so the test never races them.
    _ENV = {
        "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL": "3600",
        "RESEARCH_PLUGIN_SANDBOX_RSYNC_INTERVAL": "3600",
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = self.root / "registry.sqlite"
        self.repo_active = self.root / "active-project"
        self.repo_idle = self.root / "idle-project"
        self.routers: list[ProjectRouter] = []
        self._saved_env = {key: os.environ.get(key) for key in self._ENV}
        os.environ.update(self._ENV)

    def tearDown(self) -> None:
        for router in self.routers:
            router.shutdown()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _make_router(self) -> ProjectRouter:
        router = ProjectRouter(
            registry_db_path=self.registry,
            execution_backend_factory=_reaper_capable_backend_factory,
        )
        self.routers.append(router)
        return router

    def _seed_expired_running_sandbox(self, router: ProjectRouter) -> tuple[str, str]:
        """One project with a running-but-expired sandbox, one idle project."""
        project = router.create_project(repo_root=self.repo_active, name="Active")
        router.create_project(repo_root=self.repo_idle, name="Idle")
        app = router.app_for_project(project["id"])
        app.sandboxes.worker.rsync_syncer = FakeRsyncSyncer(
            sync_pulled=0,
            push_pulled=0,
            duration_seconds=0.0,
            command_count=1,
            sync_stdout="",
            push_stdout="",
        )
        exp_id = app.call_tool(
            "experiment.create",
            {"project_id": project["id"], "name": "restart-coverage", "intent": "restart coverage"},
        )["id"]
        with app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,)
            )
        result = app.call_tool(
            "sandbox.request", {"project_id": project["id"], "experiment_id": exp_id}
        )
        self.assertEqual(result["status"], "running")
        with app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at = ? WHERE experiment_id = ?",
                ("2000-01-01T00:00:00Z", exp_id),
            )
        return project["id"], exp_id

    def test_restart_resumes_reaper_for_project_with_running_sandbox(self) -> None:
        first = self._make_router()
        project_id, exp_id = self._seed_expired_running_sandbox(first)
        first.shutdown()

        restarted = self._make_router()
        # The project holding a running sandbox is instantiated eagerly (so its
        # reaper exists); the idle project stays lazy.
        self.assertIn(self.repo_active.resolve(), restarted._apps_by_repo)
        self.assertNotIn(self.repo_idle.resolve(), restarted._apps_by_repo)

        app = restarted._apps_by_repo[self.repo_active.resolve()]
        self.assertIsNotNone(app.sandboxes.daemons.reaper_thread)
        self.assertTrue(app.sandboxes.daemons.reaper_thread.is_alive())

        # What the reaper thread will do on its next tick: terminate the
        # expired sandbox and put the experiment back where the agent can act.
        app.sandboxes.worker.rsync_syncer = FakeRsyncSyncer(
            sync_pulled=0,
            push_pulled=0,
            duration_seconds=0.0,
            command_count=1,
            sync_stdout="",
            push_stdout="",
        )
        self.assertEqual(app.sandboxes.reap_expired(), 1)
        sandbox = app.call_tool(
            "sandbox.get", {"project_id": project_id, "experiment_id": exp_id}
        )
        self.assertEqual(sandbox["status"], "terminated")
        state = app.call_tool(
            "experiment.get_state", {"project_id": project_id, "experiment_id": exp_id}
        )
        self.assertEqual(state["status"], "ready_to_run")

    def test_restart_stays_lazy_when_no_sandboxes_are_active(self) -> None:
        first = self._make_router()
        project_id, exp_id = self._seed_expired_running_sandbox(first)
        app = first.app_for_project(project_id)
        app.call_tool(
            "sandbox.release", {"project_id": project_id, "experiment_id": exp_id}
        )
        first.shutdown()

        restarted = self._make_router()
        self.assertEqual(restarted._apps_by_repo, {})


if __name__ == "__main__":
    unittest.main()
