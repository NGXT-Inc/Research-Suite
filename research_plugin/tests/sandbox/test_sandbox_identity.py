from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend


class SandboxIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.project_id = self.app.call_tool("project.create", {"name": "Sandbox IDs"})["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _experiment(self, name: str) -> str:
        exp_id = self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": name, "intent": "x"},
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?",
                (exp_id,),
            )
        return exp_id

    def _request(self, experiment_id: str) -> dict:
        return self.app.call_tool(
            "sandbox.request",
            {"project_id": self.project_id, "experiment_id": experiment_id},
        )

    def _attachment(self, sandbox_uid: str, experiment_id: str):
        conn = self.app.store.connect()
        try:
            return conn.execute(
                """
                SELECT sandbox_uid, experiment_id, detached_at
                FROM sandbox_attachments
                WHERE sandbox_uid = ? AND experiment_id = ?
                """,
                (sandbox_uid, experiment_id),
            ).fetchone()
        finally:
            conn.close()

    def _seed_row(self, *, experiment_id: str, sandbox_uid: str, status: str, seq: int) -> None:
        now = "2026-01-01T00:00:00Z"
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, experiment_id, project_id, status,
                  created_at, updated_at, created_seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sandbox_uid, experiment_id, self.project_id, status, now, now, seq),
            )

    def test_experiment_read_prefers_running_then_newest_of_any(self) -> None:
        # An older provisioning sibling must NOT shadow a newer terminal row when
        # nothing is running: reads resolve running -> newest-of-any.
        exp = self._experiment("multi")
        self._seed_row(experiment_id=exp, sandbox_uid="uid_old", status="provisioning", seq=1)
        self._seed_row(experiment_id=exp, sandbox_uid="uid_new", status="failed", seq=2)
        self.assertEqual(
            self.app.sandboxes.registry.load_row(experiment_id=exp)["sandbox_uid"],
            "uid_new",
        )
        # A running row always wins, regardless of recency.
        self._seed_row(experiment_id=exp, sandbox_uid="uid_run", status="running", seq=3)
        self.assertEqual(
            self.app.sandboxes.registry.load_row(experiment_id=exp)["sandbox_uid"],
            "uid_run",
        )

    def test_sandbox_uid_is_unique_and_stable_across_upserts(self) -> None:
        exp_a = self._experiment("exp-a")
        exp_b = self._experiment("exp-b")
        self._request(exp_a)
        self._request(exp_b)
        uid_a = str(self.app.sandboxes.registry.load_row(experiment_id=exp_a)["sandbox_uid"])
        uid_b = str(self.app.sandboxes.registry.load_row(experiment_id=exp_b)["sandbox_uid"])

        self.assertNotEqual(uid_a, uid_b)
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_a, sandbox_uid=uid_a, detail="refreshed"
        )
        self.assertEqual(
            self.app.sandboxes.registry.load_row(experiment_id=exp_a)["sandbox_uid"],
            uid_a,
        )

    def test_attachment_created_on_request_and_closed_on_release(self) -> None:
        exp_id = self._experiment("exp-attach")
        self._request(exp_id)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        sandbox_uid = str(row["sandbox_uid"])

        attachment = self._attachment(sandbox_uid, exp_id)
        self.assertIsNotNone(attachment)
        self.assertIsNone(attachment["detached_at"])

        self.app.call_tool(
            "sandbox.release",
            {
                "project_id": self.project_id,
                "experiment_id": exp_id,
                "confirm_retained": True,
            },
        )
        attachment = self._attachment(sandbox_uid, exp_id)
        self.assertIsNotNone(attachment["detached_at"])

    def test_experiment_lookup_still_returns_the_one_sandbox(self) -> None:
        exp_id = self._experiment("exp-lookup")
        self._request(exp_id)
        rows = self.app.sandboxes.registry.list_by_experiment(experiment_id=exp_id)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["experiment_id"], exp_id)
        by_uid = self.app.sandboxes.registry.get_by_uid(
            sandbox_uid=str(rows[0]["sandbox_uid"])
        )
        self.assertEqual(by_uid["experiment_id"], exp_id)


if __name__ == "__main__":
    unittest.main()
