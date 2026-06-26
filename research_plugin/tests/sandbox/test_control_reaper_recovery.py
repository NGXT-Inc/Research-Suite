"""Cloud reaper crash recovery (cloud plan Phase 8, risk 6).

The split beta runs real billing VMs with the daemon-side reaper OFF, so a
control restart with no recovery would leave Lambda VMs unreaped. The control
entrypoint scans tenant sandbox rows on startup and resumes reaping — the cloud
analog of project_router._resume_active_sandbox_projects. This test seeds a
running, already-expired sandbox into a control store, restarts the control app
over the SAME store, and asserts the resumed reaper reaps it.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.composition.control_mode import build_control_app
from backend.config import MGMT_KEY_PATH_ENV_VAR, MGMT_PUBLIC_KEY_ENV_VAR
from backend.execution.backends.fake import FakeSandboxBackend
from backend.sandbox.sandbox_backend import BackendCapabilities


def _mounted_mgmt_key_env(root: Path) -> dict[str, str]:
    key_path = root / "managed_key"
    key_path.write_text("PRIVATE KEY\n", encoding="utf-8")
    key_path.chmod(0o600)
    return {
        MGMT_KEY_PATH_ENV_VAR: str(key_path),
        MGMT_PUBLIC_KEY_ENV_VAR: "ssh-ed25519 AAAAmanaged",
    }


def _reaper_capable_backend(*, alive_ids: tuple[str, ...] = ()) -> FakeSandboxBackend:
    backend = FakeSandboxBackend()
    backend.capabilities = BackendCapabilities(
        name="fake", enforce_expiry=True
    )
    # Pre-mark VMs the restarted backend should treat as still up (the risk-6
    # case: a billing VM the cloud forgot to reap). reconcile then keeps the row
    # running so the resumed reaper reaps it on its expiry deadline.
    for sandbox_id in alive_ids:
        backend.alive[sandbox_id] = True
    return backend


class ControlReaperRecoveryTest(unittest.TestCase):
    _ENV = {
        # Keep the reaper inert between ticks so the test drives reaping itself.
        "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL": "3600",
        "RESEARCH_PLUGIN_MODE": "control",
        # Short task-result wait so a queued daemon task that briefly outruns
        # the drain thread falls through quickly instead of holding the test 30s.
        "RESEARCH_PLUGIN_TASK_RESULT_TIMEOUT": "3",
    }

    def _await(self, predicate, timeout: float = 8.0) -> None:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("condition not reached before timeout")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in self._ENV}
        os.environ.update(self._ENV)
        self._apps: list = []

    def tearDown(self) -> None:
        if getattr(self, "_stop_drain", None) is not None:
            self._stop_drain.set()
        for t in getattr(self, "_drain_threads", []):
            t.join(timeout=2.0)
        for app in self._apps:
            app.shutdown()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _build(self, *, alive_ids: tuple[str, ...] = ()):
        app, queue = build_control_app(
            repo_root=self.staging,
            env=_mounted_mgmt_key_env(self.staging),
            execution_backend=_reaper_capable_backend(alive_ids=alive_ids),
        )
        self._apps.append(app)
        # Drain the cloud→daemon task queue immediately in a stub daemon loop so
        # teardown side work completes fast when a present daemon can answer.
        self._drain_queue(queue)
        return app, queue

    def _drain_queue(self, queue) -> None:
        import threading

        def loop():
            while not self._stop_drain.is_set():
                task = queue.poll(wait_seconds=0.2)
                if task is None:
                    continue
                queue.ack(task_id=task["id"], ok=True, result={})

        self._stop_drain = getattr(self, "_stop_drain", None) or threading.Event()
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self._drain_threads = getattr(self, "_drain_threads", [])
        self._drain_threads.append(t)

    def _seed_expired_running_sandbox(self, app) -> tuple[str, str, str]:
        # Seed a running, already-expired sandbox row directly (the provision
        # handshake is exercised elsewhere; here we test crash recovery of an
        # existing running row). The registry is the cloud's row authority.
        project_id = app.call_tool("project.create", {"name": "Cloud Project"})["id"]
        exp_id = app.call_tool(
            "experiment.create",
            {"project_id": project_id, "name": "reaper-recovery", "intent": "x"},
        )["id"]
        with app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'running' WHERE id = ?", (exp_id,)
            )
        sandbox_uid = "uid_recovery"
        app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            project_id=project_id,
            sandbox_id="sb-recovery",
            status="running",
            ssh_host="r1.fake.host",
            ssh_port=22,
            ssh_user="root",
            sync_dir="/workspace/reaper-recovery",
            expires_at="2000-01-01T00:00:00Z",
        )
        return project_id, exp_id, sandbox_uid

    def test_control_restart_resumes_reaping_of_running_rows(self) -> None:
        self._stop_drain = None  # reset per test
        first, _q = self._build()
        project_id, exp_id, sandbox_uid = self._seed_expired_running_sandbox(first)
        first.shutdown()
        self._apps.remove(first)

        # Restart the control app over the SAME store: build_control_app runs
        # the crash-recovery scan, which reconciles the still-up VM and resumes
        # the reaper, kicking one reap off-thread. The drain thread acts as a
        # present daemon for any queued side work. The VM is still up
        # (alive_ids), so reconcile keeps the row running and the resumed reaper
        # reaps it on its expiry deadline.
        restarted, _q2 = self._build(alive_ids=("sb-recovery",))
        # The experiment is reverted at the END of the reap (after terminate),
        # so wait on the experiment status — it implies the row was reaped too.
        self._await(
            lambda: restarted.call_tool(
                "experiment.get_state",
                {"project_id": project_id, "experiment_id": exp_id},
            )["status"]
            == "ready_to_run"
        )
        sandbox = restarted.call_tool(
            "sandbox.get",
            {
                "project_id": project_id,
                "experiment_id": exp_id,
                "sandbox_uid": sandbox_uid,
            },
        )
        self.assertEqual(sandbox["status"], "terminated")


if __name__ == "__main__":
    unittest.main()
