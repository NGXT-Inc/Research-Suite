from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.utils import ValidationError


class ProjectToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def test_project_view_omits_legacy_hard_stop_fields(self) -> None:
        # The hard-stop mechanism is gone; views must not resurface its
        # legacy columns even on databases that still carry them.
        project = self.call("project.create", name="Alpha")
        self.assertNotIn("hard_stop_reflection_id", project)
        self.assertNotIn("hard_stop_rationale", project)
        self.assertNotIn("stopped_at", project)

        fetched = self.call("project.get", project_id=project["id"])
        self.assertNotIn("hard_stop_reflection_id", fetched)
        self.assertNotIn("hard_stop_rationale", fetched)
        self.assertNotIn("stopped_at", fetched)

    def test_legacy_stopped_project_reactivates_on_migration(self) -> None:
        project = self.call("project.create", name="Alpha")
        # Simulate a database stopped under the removed hard-stop contract,
        # before the reactivation migration existed.
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE projects SET status = 'stopped' WHERE id = ?",
                (project["id"],),
            )
            conn.execute(
                "DELETE FROM schema_migrations WHERE name = 'reactivate_hard_stopped_projects'"
            )
        self.app.store._initialize()
        fetched = self.call("project.get", project_id=project["id"])
        self.assertEqual(fetched["status"], "active")

    def test_project_name_must_be_at_least_three_chars_on_create_and_update(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.call("project.create", name="ab")
        self.assertIn("at least 3", str(ctx.exception))

        project = self.call("project.create", name="Alpha")
        with self.assertRaises(ValidationError) as empty_ctx:
            self.call("project.update", project_id=project["id"], name=" ")
        self.assertIn("name is required", str(empty_ctx.exception))

        with self.assertRaises(ValidationError) as short_ctx:
            self.call("project.update", project_id=project["id"], name="xy")
        self.assertIn("at least 3", str(short_ctx.exception))

        updated = self.call("project.update", project_id=project["id"], name="Beta")
        self.assertEqual(updated["name"], "Beta")

    def test_hidden_project_is_stashed_from_list_but_retained(self) -> None:
        keep = self.call("project.create", name="Keep")
        stash = self.call("project.create", name="Stash")

        self.call("project.update", project_id=stash["id"], hidden=True)

        # project.list (the UI project picker) omits the hidden project...
        listed = {p["id"] for p in self.call("project.list")["projects"]}
        self.assertIn(keep["id"], listed)
        self.assertNotIn(stash["id"], listed)

        # ...but the row and direct-by-id access are fully retained.
        fetched = self.call("project.get", project_id=stash["id"])
        self.assertEqual(fetched["name"], "Stash")
        self.assertTrue(fetched["settings"]["hidden"])

        # Restoring returns it to the list (reversible).
        self.call("project.update", project_id=stash["id"], hidden=False)
        restored = {p["id"] for p in self.call("project.list")["projects"]}
        self.assertIn(stash["id"], restored)

    def test_update_without_hidden_leaves_hidden_unchanged(self) -> None:
        project = self.call("project.create", name="Alpha")
        self.call("project.update", project_id=project["id"], hidden=True)
        self.call("project.update", project_id=project["id"], summary="edited")
        fetched = self.call("project.get", project_id=project["id"])
        self.assertTrue(fetched["settings"]["hidden"])
        self.assertEqual(fetched["summary"], "edited")


if __name__ == "__main__":
    unittest.main()
