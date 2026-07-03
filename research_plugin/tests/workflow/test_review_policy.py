"""The require_verified_reviews policy knob and its gate enforcement."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.state.store import next_created_seq
from backend.utils import WorkflowError, new_id, now_iso

VALID_PLAN = (
    "## Summary\n"
    "A toy experiment used by the review-policy tests.\n\n"
    "## Objective & hypothesis\n"
    "Test that the threshold rule beats the majority baseline.\n\n"
    "## Evaluation\n"
    "Metric: accuracy vs the majority-class baseline; success if accuracy > 0.6.\n"
)


class ReviewPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project.create", name="Policy Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    # ---- helpers ----

    def _drive_to_design_review(self, *, name: str = "exp-policy") -> str:
        exp_id = self.call(
            "experiment.create", name=name, project_id=self.project_id, intent="Policy."
        )["id"]
        (self.repo / "plan.md").write_text(VALID_PLAN)
        res = self.call(
            "resource.register_file", project_id=self.project_id, path="plan.md", kind="plan"
        )
        self.call(
            "resource.associate",
            project_id=self.project_id,
            resource_id=res["id"],
            target_type="experiment",
            target_id=exp_id,
            role="plan",
        )
        self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition="submit_design",
        )
        return exp_id

    def _insert_attested_pass(self, *, exp_id: str, role: str) -> None:
        """Simulate a legacy pass whose session predates mandatory caller_session_id."""
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )
        request_id = req["review_request_id"]
        with self.app.reviews.store.transaction() as conn:
            row = conn.execute(
                "SELECT target_snapshot_id FROM review_requests WHERE id = ?", (request_id,)
            ).fetchone()
            session_id = new_id(prefix="rvs")
            conn.execute(
                """
                INSERT INTO review_sessions (
                  id, request_id, declared_agent, caller_session_id, tenant_id,
                  independence, status, created_at
                )
                VALUES (?, ?, '', '', 'local', 'attested_agent_review', 'submitted', ?)
                """,
                (session_id, request_id, now_iso()),
            )
            conn.execute(
                """
                INSERT INTO reviews (
                  id, project_id, request_id, session_id, target_snapshot_id,
                  target_type, target_id, role, verdict, return_to, notes,
                  findings_json, evidence_json, created_at, created_seq
                )
                VALUES (?, ?, ?, ?, ?, 'experiment', ?, ?, 'pass', '', '', '[]', '{}', ?, ?)
                """,
                (
                    new_id(prefix="rev"),
                    self.project_id,
                    request_id,
                    session_id,
                    row["target_snapshot_id"],
                    exp_id,
                    role,
                    now_iso(),
                    next_created_seq(conn=conn, table="reviews"),
                ),
            )
            conn.execute(
                "UPDATE review_requests SET status = 'submitted' WHERE id = ?", (request_id,)
            )

    def _pass_verified_review(self, *, exp_id: str, role: str) -> None:
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

    # ---- default (knob off) ----

    def test_attested_pass_satisfies_gate_by_default(self) -> None:
        exp_id = self._drive_to_design_review()
        self._insert_attested_pass(exp_id=exp_id, role="design_reviewer")
        out = self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition="mark_ready_to_run",
        )
        self.assertEqual(out["status"], "ready_to_run")

    # ---- knob on ----

    def test_policy_blocks_attested_pass_with_clear_reason(self) -> None:
        self.call("project.update", project_id=self.project_id, require_verified_reviews=True)
        exp_id = self._drive_to_design_review()
        self._insert_attested_pass(exp_id=exp_id, role="design_reviewer")
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.transition",
                project_id=self.project_id,
                experiment_id=exp_id,
                transition="mark_ready_to_run",
            )
        self.assertIn("require_verified_reviews", str(ctx.exception))
        self.assertIn("caller_session_id", str(ctx.exception))

    def test_workflow_surfaces_attested_blocked_reason(self) -> None:
        self.call("project.update", project_id=self.project_id, require_verified_reviews=True)
        exp_id = self._drive_to_design_review()
        self._insert_attested_pass(exp_id=exp_id, role="design_reviewer")
        wf = self.call(
            "workflow.status_and_next", project_id=self.project_id, experiment_id=exp_id
        )
        workflow = wf["workflow"]
        self.assertEqual(workflow["current_gate"], "design_review")
        self.assertEqual(workflow["review_gate"]["status"], "attested_blocked")
        self.assertTrue(
            any("require_verified_reviews" in item for item in workflow["missing_evidence"])
        )
        # The remedy is a fresh, verified review — review.request stays allowed.
        self.assertIn("review.request", workflow["allowed_actions"])

    def test_verified_review_satisfies_gate_with_policy_on(self) -> None:
        self.call("project.update", project_id=self.project_id, require_verified_reviews=True)
        exp_id = self._drive_to_design_review()
        self._insert_attested_pass(exp_id=exp_id, role="design_reviewer")
        self._pass_verified_review(exp_id=exp_id, role="design_reviewer")
        out = self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition="mark_ready_to_run",
        )
        self.assertEqual(out["status"], "ready_to_run")

    def test_policy_can_be_switched_back_off(self) -> None:
        self.call("project.update", project_id=self.project_id, require_verified_reviews=True)
        self.call("project.update", project_id=self.project_id, require_verified_reviews=False)
        exp_id = self._drive_to_design_review()
        self._insert_attested_pass(exp_id=exp_id, role="design_reviewer")
        out = self.call(
            "experiment.transition",
            project_id=self.project_id,
            experiment_id=exp_id,
            transition="mark_ready_to_run",
        )
        self.assertEqual(out["status"], "ready_to_run")

    # ---- settings surface ----

    def test_project_update_roundtrips_the_knob(self) -> None:
        updated = self.call(
            "project.update", project_id=self.project_id, require_verified_reviews=True
        )
        self.assertTrue(updated["settings"]["require_verified_reviews"])
        fetched = self.call("project.get", project_id=self.project_id)
        self.assertTrue(fetched["settings"]["require_verified_reviews"])
        # Unrelated updates leave the knob alone.
        renamed = self.call("project.update", project_id=self.project_id, name="Policy Renamed")
        self.assertTrue(renamed["settings"]["require_verified_reviews"])


if __name__ == "__main__":
    unittest.main()
