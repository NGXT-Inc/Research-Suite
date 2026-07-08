from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.utils import ValidationError


class ResourceVersioningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project = self.call("project.create", name="Version Test")
        self.project_id = self.project["id"]
        self.exp = self.call("experiment.create", name="exp-1", project_id=self.project_id, intent="Track plan history.")
        self.exp_id = self.exp["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def test_associate_snapshots_changed_live_file_without_explicit_sync(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("version one\n")
        plan = self.call("resource.register", project_id=self.project_id, path="plan.md", kind="note")
        first_id = plan["current_version_id"]
        first_version = self.call("resource.find", project_id=self.project_id, resource_id=plan["id"], include_history=True)["versions"][0]

        plan_path.write_text("version two\n")
        associated = self.call(
            "resource.register",
            project_id=self.project_id,
            resource_id=plan["id"],
            target_type="experiment",
            target_id=self.exp_id,
            role="plan",
        )

        second_id = associated["current_version_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(associated["associations"][0]["version_id"], second_id)
        history = self.call("resource.find", project_id=self.project_id, resource_id=plan["id"], include_history=True)
        second_version = history["versions"][-1]
        self.assertEqual(len(history["versions"]), 2)
        # New version must reflect new file contents — sha256 differs.
        self.assertNotEqual(first_version["content_sha256"], second_version["content_sha256"])

    def test_passing_review_survives_live_edits_after_submission(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text(
            "## Summary\nApproved plan.\n\n"
            "## Objective & hypothesis\nTest the claim.\n\n"
            "## Evaluation\nMetric: accuracy vs baseline.\n"
        )
        plan = self.call("resource.register", project_id=self.project_id, path="plan.md", kind="plan")
        self.call("resource.register", project_id=self.project_id, resource_id=plan["id"], target_type="experiment", target_id=self.exp_id, role="plan")
        first_id = plan["current_version_id"]

        self.call("experiment.transition", project_id=self.project_id, experiment_id=self.exp_id, transition="submit_design")
        request = self.call("review.request", project_id=self.project_id, target_type="experiment", target_id=self.exp_id, role="design_reviewer")
        self.assertEqual(request["target_snapshot"]["resources"][0]["version_id"], first_id)
        self.assertEqual(request["target_snapshot"]["resources"][0]["role"], "plan")
        session = self.call(
            "review.start",
            review_request_id=request["review_request_id"],
            reviewer_capability=request["reviewer_capability"],
            caller_session_id="reviewer",
        )
        review = self.call(
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict="pass",
            synopsis="The plan is scoped correctly and the resource version snapshot pins as expected.",
        )
        self.assertEqual(review["target_snapshot"]["resources"][0]["version_id"], first_id)
        review_status = self.call("review.status", project_id=self.project_id, target_type="experiment", target_id=self.exp_id)
        self.assertEqual(review_status["requests"][0]["target_snapshot"]["resources"][0]["version_id"], first_id)
        self.assertEqual(review_status["reviews"][0]["target_snapshot"]["resources"][0]["version_id"], first_id)
        self.assertEqual(
            self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=self.exp_id)["workflow"]["current_gate"],
            "design_review_passed",
        )

        # Submission semantics (cloud plan Phase 2): editing the live file
        # after review does NOT perturb the pinned association or the passing
        # review — the working tree may diverge; the record stands.
        plan_path.write_text("changed plan after review\n")
        status = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=self.exp_id)
        current_plan = status["experiment"]["current_attempt_resources"][0]

        self.assertNotIn("resource_refresh", status)
        self.assertEqual(current_plan["association_version_id"], first_id)
        self.assertEqual(current_plan["association_role"], "plan")
        self.assertEqual(status["workflow"]["current_gate"], "design_review_passed")

    def test_deleting_live_file_after_submission_does_not_crash_status(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("planned\n")
        plan = self.call("resource.register", project_id=self.project_id, path="plan.md", kind="plan")
        self.call("resource.register", project_id=self.project_id, resource_id=plan["id"], target_type="experiment", target_id=self.exp_id, role="plan")

        # Submission semantics: the gates lint the submitted bytes, so the
        # association survives the live file vanishing. The content here lacks
        # the plan spine, so the guidance pre-lint reports plan_invalid.
        plan_path.unlink()
        status = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=self.exp_id)

        self.assertNotIn("resource_refresh", status)
        self.assertEqual(status["experiment"]["current_attempt_resources"][0]["missing"], 0)
        self.assertEqual(status["workflow"]["current_gate"], "plan_invalid")

    def test_delete_removes_resource_from_active_tracking_but_preserves_history(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("planned\n")
        plan = self.call("resource.register", project_id=self.project_id, path="plan.md", kind="plan")
        self.call("resource.register", project_id=self.project_id, resource_id=plan["id"], target_type="experiment", target_id=self.exp_id, role="plan")

        deleted = self.call("resource.delete", project_id=self.project_id, resource_id=plan["id"])
        resources = self.call("resource.find", project_id=self.project_id)["resources"]
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=self.exp_id)
        history = self.call("resource.find", project_id=self.project_id, resource_id=plan["id"], include_history=True)

        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["removed_associations"], 1)
        self.assertEqual(deleted["resource"]["deleted"], 1)
        self.assertEqual(resources, [])
        self.assertEqual(state["current_attempt_resources"], [])
        self.assertEqual(len(history["versions"]), 1)

    def test_registering_deleted_resource_revives_same_resource_id(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("version one\n")
        plan = self.call("resource.register", project_id=self.project_id, path="plan.md", kind="plan")
        self.call("resource.delete", project_id=self.project_id, resource_id=plan["id"])

        plan_path.write_text("version two\n")
        revived = self.call("resource.register", project_id=self.project_id, path="plan.md", kind="plan")
        resources = self.call("resource.find", project_id=self.project_id)["resources"]

        self.assertEqual(revived["id"], plan["id"])
        self.assertEqual(revived["deleted"], 0)
        self.assertEqual(revived["missing"], 0)
        self.assertEqual(len(revived["associations"]), 0)
        self.assertEqual([item["id"] for item in resources], [plan["id"]])
        self.assertEqual(len(self.call("resource.find", project_id=self.project_id, resource_id=plan["id"], include_history=True)["versions"]), 2)

    def test_same_repo_file_is_a_distinct_resource_per_project(self) -> None:
        (self.repo / "shared.md").write_text("shared\n")
        other = self.call("project.create", name="Other Project")
        first = self.call("resource.register", project_id=self.project_id, path="shared.md", kind="note")
        second = self.call("resource.register", project_id=other["id"], path="shared.md", kind="note")

        self.assertNotEqual(first["id"], second["id"])
        first_list = self.call("resource.find", project_id=self.project_id)["resources"]
        second_list = self.call("resource.find", project_id=other["id"])["resources"]
        self.assertEqual([r["id"] for r in first_list], [first["id"]])
        self.assertEqual([r["id"] for r in second_list], [second["id"]])

    def test_content_change_with_restored_mtime_is_detected_at_resubmission(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("AAAA\n")
        plan = self.call("resource.register", project_id=self.project_id, path="plan.md", kind="plan")
        self.call("resource.register", project_id=self.project_id, resource_id=plan["id"], target_type="experiment", target_id=self.exp_id, role="plan")
        first_id = plan["current_version_id"]
        original = plan_path.stat()

        # Same byte length, different content, with mtime restored to the value
        # captured at registration. Only ctime distinguishes the edit. The
        # observation happens at RE-ASSOCIATE (submission) — there is no
        # background sweep — and the ctime-bearing version token must still
        # catch the sneaky edit there.
        plan_path.write_text("BBBB\n")
        os.utime(plan_path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(plan_path.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(plan_path.stat().st_size, original.st_size)

        resubmitted = self.call(
            "resource.register",
            project_id=self.project_id,
            resource_id=plan["id"],
            target_type="experiment",
            target_id=self.exp_id,
            role="plan",
        )
        self.assertNotEqual(resubmitted["current_version_id"], first_id)
        self.assertEqual(
            resubmitted["associations"][0]["version_id"],
            resubmitted["current_version_id"],
        )

    def test_internal_state_directory_cannot_be_registered_as_resource(self) -> None:
        internal = self.repo / ".research_plugin" / "note.md"
        internal.parent.mkdir(exist_ok=True)
        internal.write_text("internal\n")
        with self.assertRaises(ValidationError):
            self.call("resource.register", project_id=self.project_id, path=".research_plugin/note.md")


if __name__ == "__main__":
    unittest.main()
