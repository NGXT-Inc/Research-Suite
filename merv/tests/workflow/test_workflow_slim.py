"""The agent-facing `workflow.status_and_next` tool returns a slim projection;
the service method still returns the full shape the UI depends on."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend


# association_version_id is the submission pin — agents confirm a
# re-associate took effect by watching it change, so it stays in the slim view.
SLIM_RESOURCE_KEYS = {
    "id", "association_role", "association_version_id", "path", "kind",
    "missing", "size_bytes",
}
HEAVY_RESOURCE_KEYS = {"version_token", "mtime_ns", "current_version_id", "git_commit"}

class WorkflowSlimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.project_id = self.call("project", action="create", name="Slim Project")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _set_status(self, exp_id: str, status: str) -> None:
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = ? WHERE id = ?", (status, exp_id))

    def _experiment_with_plan(self) -> str:
        exp_id = self.call(
            "experiment.create",
            name="the-thing",
            project_id=self.project_id,
            intent="Do the thing on the staged subset.\n\nTitle: The Thing",
        )["id"]
        (self.repo / "plan.md").write_text("planned\n")
        self.call(
            "resource.register", project_id=self.project_id, path="plan.md", kind="plan",
            target_type="experiment", target_id=exp_id, role="plan",
        )
        return exp_id

    def test_experiment_scope_is_slim(self) -> None:
        exp_id = self._experiment_with_plan()
        slim = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id)

        self.assertEqual(slim["scope"], "experiment")
        self.assertIn("current_gate", slim["workflow"])

        exp = slim["experiment"]
        # The agent sees the experiment's identity: its name.
        self.assertEqual(exp["name"], "the-thing")
        # The duplicate all-attempts `resources` list is gone…
        self.assertNotIn("resources", exp)
        self.assertIn("current_attempt_resources", exp)
        # …and each resource carries only the light fields.
        res = exp["current_attempt_resources"][0]
        self.assertEqual(set(res), SLIM_RESOURCE_KEYS)
        self.assertEqual(HEAVY_RESOURCE_KEYS & set(res), set())
        self.assertEqual(res["association_role"], "plan")
        # tested_claims collapsed to ids; reviews compacted.
        self.assertIn("tested_claim_ids", exp)
        self.assertNotIn("tested_claims", exp)
        self.assertIsInstance(exp["reviews"], list)

        # Project block is a bare reference — no other experiments' intents.
        self.assertEqual(set(slim["project"]), {"id", "name"})

        # No sandbox yet → explicitly says so.
        self.assertFalse(slim["sandbox"]["active"])
        self.assertIn("note", slim["sandbox"])

    def test_active_sandbox_is_summarized(self) -> None:
        exp_id = self._experiment_with_plan()
        self._set_status(exp_id, "ready_to_run")
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id, gpu="A100")

        slim = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id)
        sandbox = slim["sandbox"]
        self.assertTrue(sandbox["active"])
        self.assertTrue(sandbox["sandbox_id"])
        self.assertTrue(sandbox["ssh_host"])
        self.assertEqual(sandbox["status"], "running")
        # SSH key material / raw command are NOT here — that's sandbox.request's job.
        self.assertNotIn("key_path", sandbox)

    def test_project_scope_is_compact(self) -> None:
        # With no experiment yet, the tool orients at the project level
        # (`_resolve_scope` only auto-picks an experiment once one exists).
        self.call("claim.create", project_id=self.project_id, statement="Bigger batches help.")
        slim = self.call("workflow.status_and_next", project_id=self.project_id)

        self.assertEqual(slim["scope"], "project")
        self.assertIsNone(slim["experiment"])
        self.assertEqual(slim["workflow"]["current_gate"], "project_setup")
        claim = slim["project"]["claims"][0]
        self.assertEqual(set(claim), {"id", "status", "confidence", "statement"})

    def test_service_method_keeps_full_shape_for_ui(self) -> None:
        exp_id = self._experiment_with_plan()
        full = self.app.workflow.status_and_next(project_id=self.project_id, experiment_id=exp_id)
        # The UI path still gets the rich shape: all-attempts resources with
        # version bookkeeping, and the project-wide experiment list.
        self.assertIn("resources", full["experiment"])
        self.assertIn("version_token", full["experiment"]["current_attempt_resources"][0])
        self.assertIn("active_experiments", full["project"])


if __name__ == "__main__":
    unittest.main()
