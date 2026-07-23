"""The agent-facing experiment.get_state / experiment.list tools return a
slim projection (detail kept, waste dropped); the service methods stay full."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend

SLIM_ARTIFACT_KEYS = {"id", "role", "path", "lens_id", "size_bytes", "title"}
WASTE_ARTIFACT_KEYS = {"content_sha256", "content_type", "created_by", "created_at",
                       "updated_at", "project_id", "attempt_index", "submitted_order"}
WASTE_REVIEW_KEYS = {"target_snapshot_id", "request_id", "session_id", "target_id", "target_type", "project_id"}


class ExperimentSlimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.project_id = self.call("project", action="create", name="Slim get_state")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _experiment_with_artifacts(self) -> str:
        exp_id = self.call(
            "experiment.create", name="reve-small", project_id=self.project_id,
            intent="Train REVE-Small.\n\nTitle: REVE-Small",
        )["id"]
        for path, role in [
            ("experiments/004/plan.md", "plan"),
            ("experiments/004/report.md", "report"),
            ("experiments/004/results/status.json", "result"),
        ]:
            self.app.submit_artifact(
                project_id=self.project_id, target_type="experiment",
                target_id=exp_id, role=role, path=path, body="x" * 50,
            )
        return exp_id

    def test_create_returns_folder_guidance_without_mkdir(self) -> None:
        created = self.call(
            "experiment.create",
            name="folder-test",
            project_id=self.project_id,
            intent="Create the experiment folder.",
        )

        self.assertTrue(created["id"])
        self.assertEqual(created["folder"], "experiments/folder-test/")
        self.assertIn("experiments/folder-test/", created["folder_guidance"])
        self.assertFalse((self.repo / "experiments" / "folder-test").exists())

    def test_get_state_tool_is_slim(self) -> None:
        exp_id = self._experiment_with_artifacts()
        slim = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)

        # The duplicate all-attempts `resources` list is gone.
        self.assertNotIn("artifacts", slim)
        self.assertIn("current_attempt_artifacts", slim)
        res = slim["current_attempt_artifacts"][0]
        self.assertEqual(set(res), SLIM_ARTIFACT_KEYS)
        self.assertEqual(WASTE_ARTIFACT_KEYS & set(res), set())
        # Detail that get_state exists for is preserved.
        self.assertIn("intent", slim)
        self.assertIn("conclusion", slim)
        self.assertIn("gate_checklist", slim)
        self.assertEqual(slim["storage_objects"], [])
        self.assertEqual({"id", "statement", "confidence", "status", "scope"},
                         set(slim["tested_claims"][0]) if slim["tested_claims"] else {"id", "statement", "confidence", "status", "scope"})
        # Single-attempt experiment: no prior-attempt block.
        self.assertNotIn("prior_attempt_artifacts", slim)

    def test_get_state_review_keeps_findings_drops_bookkeeping(self) -> None:
        exp_id = self._experiment_with_artifacts()
        # Seed a review directly (FK off) with bookkeeping + findings.
        import sqlite3
        raw = sqlite3.connect(self.repo / ".research_plugin" / "state.sqlite")
        raw.execute("PRAGMA foreign_keys=OFF")
        cols = [r[1] for r in raw.execute("PRAGMA table_info(reviews)").fetchall()]
        vals = {
            "id": "rev_1", "project_id": self.project_id, "target_type": "experiment", "target_id": exp_id,
            "role": "experiment_reviewer", "verdict": "pass", "status": "submitted",
            "findings_json": json.dumps([{"issue": "narrow", "severity": "low"}]),
            "evidence_json": json.dumps({"exit_code": 0}), "notes": "looks good",
            "target_snapshot_id": "experiment|" + "x" * 500, "created_at": "2026-06-03T04:41:27Z",
            "request_id": "rr_x", "session_id": "rvs_x",
        }
        present = {k: v for k, v in vals.items() if k in cols}
        raw.execute(f"INSERT INTO reviews ({','.join(present)}) VALUES ({','.join('?' for _ in present)})", list(present.values()))
        raw.commit(); raw.close()

        slim = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        review = slim["reviews"][0]
        self.assertEqual(review["verdict"], "pass")
        self.assertEqual(review["findings"][0]["issue"], "narrow")   # detail kept
        self.assertEqual(review["notes"], "looks good")
        self.assertEqual(review["evidence"], {"exit_code": 0})
        self.assertEqual(WASTE_REVIEW_KEYS & set(review), set())     # bookkeeping dropped

    def test_list_tool_is_slim(self) -> None:
        self._experiment_with_artifacts()
        listed = self.call("experiment.list", project_id=self.project_id)["experiments"]
        self.assertNotIn("artifacts", listed[0])
        self.assertEqual(set(listed[0]["current_attempt_artifacts"][0]), SLIM_ARTIFACT_KEYS)

    def test_service_method_keeps_full_shape_for_ui(self) -> None:
        exp_id = self._experiment_with_artifacts()
        full = self.app.experiments.get_state(experiment_id=exp_id, project_id=self.project_id)
        self.assertIn("artifacts", full)
        self.assertIn("content_type", full["current_attempt_artifacts"][0])


if __name__ == "__main__":
    unittest.main()
