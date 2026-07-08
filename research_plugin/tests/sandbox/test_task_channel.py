"""The control task channel records lifecycle signals without local conn IO."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.control.control_runtime import ControlTaskChannel
from backend.execution.backends.fake import FakeSandboxBackend
from backend.utils import ValidationError


class TaskChannelTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.channel: ControlTaskChannel = self.app.sandboxes.tasks
        self.project_id = self.call("project", action="create", name="Channel Project")["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _experiment(self) -> str:
        exp_id = self.call(
            "experiment.create", name="exp-1", project_id=self.project_id, intent="x"
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,)
            )
        return exp_id

    def _task_types(self) -> list[str]:
        return [task_type for task_type, _payload in self.channel.history]


class TaskChannelTest(TaskChannelTestBase):
    def test_lifecycle_tasks_dispatch_synchronously_in_order(self) -> None:
        # provision → release drives the control lifecycle; the channel must
        # observe the terminal teardown without mutating local conn files.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        self.assertEqual(self._task_types(), ["teardown"])
        self.assertEqual(len(self.channel.history), 1)

    def test_teardown_task_fires_on_terminal_rows(self) -> None:
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        _task_type, teardown = next(
            item for item in self.channel.history if item[0] == "teardown"
        )
        self.assertEqual(teardown["experiment_id"], exp_id)
        self.assertEqual(teardown["sandbox_id"], created["sandbox_id"])

    def test_reaper_terminates_without_file_transfer_task(self) -> None:
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        state = self.call(
            "experiment.get_state", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertEqual(state["status"], "ready_to_run")
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE sandbox_uid=?",
                ("2000-01-01T00:00:00Z", created["sandbox_uid"]),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        self.assertEqual(self._task_types(), ["teardown"])
        self.assertIn(created["sandbox_id"], self.backend.terminated)
        state = self.call(
            "experiment.get_state", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertEqual(state["status"], "ready_to_run")
        got = self.call(
            "sandbox.get", project_id=self.project_id, sandbox_uid=created["sandbox_uid"]
        )
        self.assertEqual(got["status"], "terminated")

    def test_endpoint_move_emits_a_conn_refresh_task(self) -> None:
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        self.backend.move_endpoint(
            sandbox_id=created["sandbox_id"], host="r999.modal.host", port=55555
        )
        self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        _task_type, refresh = next(
            item for item in self.channel.history if item[0] == "conn_refresh"
        )
        self.assertEqual(refresh["row"]["ssh_host"], "r999.modal.host")
        self.assertEqual(refresh["row"]["ssh_port"], 55555)

    def test_unknown_task_type_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.channel.submit(task_type="reboot_vm", payload={})


if __name__ == "__main__":
    unittest.main()
