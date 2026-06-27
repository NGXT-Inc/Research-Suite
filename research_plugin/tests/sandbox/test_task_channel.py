"""The control→data task channel, routed in-process."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.dataplane.tasks import InProcessTaskChannel
from backend.execution.backends.fake import FakeSandboxBackend
from backend.utils import ValidationError


class TaskChannelTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.channel: InProcessTaskChannel = self.app.sandboxes.tasks
        self.project_id = self.call("project.create", name="Channel Project")["id"]

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
        return [task.type for task, _ack in self.channel.history]


class TaskChannelTest(TaskChannelTestBase):
    def test_lifecycle_tasks_dispatch_synchronously_in_order(self) -> None:
        # provision → release drives the full local loop; the channel must
        # observe the terminal teardown.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        self.assertEqual(self._task_types(), ["teardown"])
        acks = [ack for _task, ack in self.channel.history]
        self.assertTrue(all(ack["ok"] for ack in acks))
        # One ack per task, by id.
        self.assertEqual(
            [ack["task_id"] for _task, ack in self.channel.history],
            [task.id for task, _ack in self.channel.history],
        )
        self.assertEqual(len({task.id for task, _ack in self.channel.history}), 1)

    def test_teardown_task_fires_on_terminal_rows(self) -> None:
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        conn_file = (
            self.repo
            / ".research_plugin"
            / "sandboxes"
            / "conn"
            / created["sandbox_uid"]
        )
        self.assertTrue(conn_file.exists())
        self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        teardown = next(t for t, _a in self.channel.history if t.type == "teardown")
        self.assertEqual(teardown.payload["experiment_id"], exp_id)
        self.assertEqual(teardown.payload["sandbox_id"], created["sandbox_id"])
        # The conn file is gone, so sbx fails loudly for the released sandbox.
        self.assertFalse(conn_file.exists())

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
        refresh = next(t for t, _a in self.channel.history if t.type == "conn_refresh")
        self.assertEqual(refresh.payload["row"]["ssh_host"], "r999.modal.host")
        # The task re-rendered the conn file the dispatcher sources.
        body = (
            self.repo
            / ".research_plugin"
            / "sandboxes"
            / "conn"
            / created["sandbox_uid"]
        ).read_text()
        self.assertIn("r999.modal.host", body)
        self.assertIn("55555", body)

    def test_unknown_task_type_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.channel.submit(task_type="reboot_vm", payload={})


if __name__ == "__main__":
    unittest.main()
