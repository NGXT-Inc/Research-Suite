from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.utils import ValidationError


class ResourceVersioningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project = self.call("project.create", name="Version Test")
        self.project_id = self.project["id"]
        self.exp = self.call("experiment.create", project_id=self.project_id, intent="Track plan history.")
        self.exp_id = self.exp["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def test_associate_snapshots_changed_live_file_without_explicit_sync(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("version one\n")
        plan = self.call("resource.register_file", project_id=self.project_id, path="plan.md", kind="note")
        first_id = plan["current_version_id"]
        first_version = self.call("resource.history", project_id=self.project_id, resource_id=plan["id"])["versions"][0]

        plan_path.write_text("version two\n")
        associated = self.call(
            "resource.associate",
            project_id=self.project_id,
            resource_id=plan["id"],
            target_type="experiment",
            target_id=self.exp_id,
            role="plan",
        )

        second_id = associated["current_version_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(associated["associations"][0]["version_id"], second_id)
        history = self.call("resource.history", project_id=self.project_id, resource_id=plan["id"])
        second_version = history["versions"][-1]
        self.assertEqual(len(history["versions"]), 2)
        # New version must reflect new file contents — sha256 differs.
        self.assertNotEqual(first_version["content_sha256"], second_version["content_sha256"])

    def test_status_refreshes_changed_resources_and_invalidates_old_review_snapshot(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("approved plan\n")
        plan = self.call("resource.register_file", project_id=self.project_id, path="plan.md", kind="plan")
        self.call("resource.associate", project_id=self.project_id, resource_id=plan["id"], target_type="experiment", target_id=self.exp_id, role="plan")
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
        review = self.call("review.submit", review_session_id=session["review_session_id"], verdict="pass")
        self.assertEqual(review["target_snapshot"]["resources"][0]["version_id"], first_id)
        review_status = self.call("review.status", project_id=self.project_id, target_type="experiment", target_id=self.exp_id)
        self.assertEqual(review_status["requests"][0]["target_snapshot"]["resources"][0]["version_id"], first_id)
        self.assertEqual(review_status["reviews"][0]["target_snapshot"]["resources"][0]["version_id"], first_id)
        self.assertEqual(
            self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=self.exp_id)["workflow"]["current_gate"],
            "design_review_passed",
        )

        plan_path.write_text("changed plan after review\n")
        status = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=self.exp_id)
        refreshed = status["resource_refresh"]["changed"][0]
        current_plan = status["experiment"]["current_attempt_resources"][0]

        self.assertEqual(refreshed["status"], "refreshed")
        self.assertNotEqual(refreshed["version_id"], first_id)
        self.assertEqual(current_plan["association_version_id"], refreshed["version_id"])
        self.assertEqual(status["workflow"]["current_gate"], "design_review")
        self.assertEqual(status["workflow"]["next_action"], "launch_design_reviewer")

    def test_status_marks_missing_associated_resource_without_crashing(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("planned\n")
        plan = self.call("resource.register_file", project_id=self.project_id, path="plan.md", kind="plan")
        self.call("resource.associate", project_id=self.project_id, resource_id=plan["id"], target_type="experiment", target_id=self.exp_id, role="plan")

        plan_path.unlink()
        status = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=self.exp_id)

        self.assertEqual(status["resource_refresh"]["changed"][0]["status"], "missing")
        self.assertEqual(status["experiment"]["current_attempt_resources"][0]["missing"], 1)
        self.assertEqual(status["workflow"]["current_gate"], "plan_required")

    def test_same_repo_file_is_a_distinct_resource_per_project(self) -> None:
        (self.repo / "shared.md").write_text("shared\n")
        other = self.call("project.create", name="Other Project")
        first = self.call("resource.register_file", project_id=self.project_id, path="shared.md", kind="note")
        second = self.call("resource.register_file", project_id=other["id"], path="shared.md", kind="note")

        self.assertNotEqual(first["id"], second["id"])
        first_list = self.call("resource.list", project_id=self.project_id)["resources"]
        second_list = self.call("resource.list", project_id=other["id"])["resources"]
        self.assertEqual([r["id"] for r in first_list], [first["id"]])
        self.assertEqual([r["id"] for r in second_list], [second["id"]])

    def test_content_change_with_restored_mtime_is_still_detected(self) -> None:
        plan_path = self.repo / "plan.md"
        plan_path.write_text("AAAA\n")
        plan = self.call("resource.register_file", project_id=self.project_id, path="plan.md", kind="plan")
        self.call("resource.associate", project_id=self.project_id, resource_id=plan["id"], target_type="experiment", target_id=self.exp_id, role="plan")
        first_id = plan["current_version_id"]
        original = plan_path.stat()

        # Same byte length, different content, with mtime restored to the value
        # captured at registration. Only ctime distinguishes the edit.
        plan_path.write_text("BBBB\n")
        os.utime(plan_path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(plan_path.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(plan_path.stat().st_size, original.st_size)

        status = self.call("workflow.status_and_next", project_id=self.project_id, experiment_id=self.exp_id)
        changed = status.get("resource_refresh", {}).get("changed", [])
        self.assertTrue(any(item["status"] == "refreshed" for item in changed))
        current_plan = status["experiment"]["current_attempt_resources"][0]
        self.assertNotEqual(current_plan["association_version_id"], first_id)

    def test_internal_state_directory_cannot_be_registered_as_resource(self) -> None:
        internal = self.repo / ".research_plugin" / "note.md"
        internal.parent.mkdir(exist_ok=True)
        internal.write_text("internal\n")
        with self.assertRaises(ValidationError):
            self.call("resource.register_file", project_id=self.project_id, path=".research_plugin/note.md")


if __name__ == "__main__":
    unittest.main()
