from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.utils import ValidationError, WorkflowError


class WorkflowGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project.create", name="Gate Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    # ---- helpers ----

    def _write_and_associate(self, *, exp_id: str, path: str, role: str, body: str) -> None:
        (self.repo / path).write_text(body)
        res = self.call("resource.register_file", project_id=self.project_id, path=path, kind=role)
        self.call(
            "resource.associate",
            project_id=self.project_id,
            resource_id=res["id"],
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )

    def _pass_review(self, *, exp_id: str, role: str) -> None:
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id=f"{role}-reviewer",
        )
        self.call("review.submit", review_session_id=session["review_session_id"], verdict="pass")

    def _drive_to_complete(self, *, conclusion: str = "") -> str:
        exp_id = self.call("experiment.create", project_id=self.project_id, intent="Full loop.")["id"]
        self._write_and_associate(exp_id=exp_id, path="plan.md", role="plan", body="the plan\n")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_design")
        self._pass_review(exp_id=exp_id, role="design_reviewer")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="mark_ready_to_run")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="start_running")
        self._write_and_associate(exp_id=exp_id, path="results.json", role="result", body="{\"metric\": 1}\n")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results")
        self._pass_review(exp_id=exp_id, role="experiment_reviewer")
        evidence = {"conclusion": conclusion} if conclusion else None
        self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition="complete",
            evidence=evidence,
        )
        return exp_id

    # ---- plan gate ----

    def test_submit_design_requires_a_plan_resource(self) -> None:
        exp = self.call("experiment.create", project_id=self.project_id, intent="No plan yet.")
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        # Reaching design_review must be impossible without a plan, so ready_to_run is too.
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp["id"])["status"],
            "planned",
        )
        self._write_and_associate(exp_id=exp["id"], path="plan.md", role="plan", body="plan\n")
        out = self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="submit_design")
        self.assertEqual(out["status"], "design_review")

    # ---- terminal transitions ----

    def test_terminal_experiment_rejects_abandon(self) -> None:
        exp_id = self._drive_to_complete()
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)["status"],
            "complete",
        )
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="abandon")
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="mark_failed")
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)["status"],
            "complete",
        )

    def test_abandoned_experiment_cannot_be_re_abandoned(self) -> None:
        exp = self.call("experiment.create", project_id=self.project_id, intent="Dead end.")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="abandon")
        with self.assertRaises(WorkflowError):
            self.call("experiment.transition", project_id=self.project_id, experiment_id=exp["id"], transition="abandon")

    # ---- conclusion ----

    def test_complete_persists_conclusion(self) -> None:
        exp_id = self._drive_to_complete(conclusion="The claim is supported by results.json.")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["conclusion"], "The claim is supported by results.json.")

    # ---- claim.update ----

    def test_claim_update_changes_status_and_confidence(self) -> None:
        claim = self.call("claim.create", project_id=self.project_id, statement="Bigger batch helps.")
        self.assertEqual(claim["status"], "active")
        updated = self.call(
            "claim.update",
            project_id=self.project_id,
            claim_id=claim["id"],
            status="supported",
            confidence="high",
        )
        self.assertEqual(updated["status"], "supported")
        self.assertEqual(updated["confidence"], "high")
        self.assertEqual(updated["statement"], "Bigger batch helps.")

    def test_claim_update_rejects_unknown_status(self) -> None:
        claim = self.call("claim.create", project_id=self.project_id, statement="Claim.")
        with self.assertRaises(ValidationError):
            self.call("claim.update", project_id=self.project_id, claim_id=claim["id"], status="bogus")

    def test_claim_update_is_project_scoped(self) -> None:
        claim = self.call("claim.create", project_id=self.project_id, statement="Claim.")
        other = self.call("project.create", name="Other")
        from backend.utils import NotFoundError

        with self.assertRaises(NotFoundError):
            self.call("claim.update", project_id=other["id"], claim_id=claim["id"], status="supported")


if __name__ == "__main__":
    unittest.main()
