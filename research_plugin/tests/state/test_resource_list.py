from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp


class ResourceListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project.create", name="List Test")["id"]
        self.exp_id = self.call(
            "experiment.create", name="exp-1", project_id=self.project_id, intent="list filters"
        )["id"]
        # Three resources of two kinds; one associated with the experiment.
        for name, kind in (("a.md", "note"), ("b.csv", "dataset"), ("c.py", "code")):
            (self.repo / name).write_text(f"{name} content\n")
            self.call("resource.register_file", project_id=self.project_id, path=name, kind=kind)
        self.call(
            "resource.associate", project_id=self.project_id, resource_id=self._rid("b.csv"),
            target_type="experiment", target_id=self.exp_id, role="input",
        )

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def _rid(self, path: str) -> str:
        for r in self.call("resource.list", project_id=self.project_id)["resources"]:
            if r["path"] == path:
                return r["id"]
        raise AssertionError(f"resource {path} not found")

    def test_list_returns_metadata(self) -> None:
        res = self.call("resource.list", project_id=self.project_id)
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["count"], 3)
        self.assertFalse(res["has_more"])
        self.assertFalse(res["compact"])

    def test_filter_by_kind(self) -> None:
        res = self.call("resource.list", project_id=self.project_id, kind="dataset")
        self.assertEqual([r["path"] for r in res["resources"]], ["b.csv"])
        self.assertEqual(res["total"], 1)

    def test_filter_by_experiment(self) -> None:
        res = self.call("resource.list", project_id=self.project_id, experiment_id=self.exp_id)
        self.assertEqual([r["path"] for r in res["resources"]], ["b.csv"])

    def test_filter_by_missing(self) -> None:
        # Flag a.md missing directly (the missing-DETECTION path is separate; here
        # we only exercise the list FILTER).
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE resources SET missing = 1 WHERE project_id = ? AND path = ?",
                (self.project_id, "a.md"),
            )
        present = self.call("resource.list", project_id=self.project_id, missing=False)
        missing = self.call("resource.list", project_id=self.project_id, missing=True)
        self.assertNotIn("a.md", [r["path"] for r in present["resources"]])
        self.assertEqual([r["path"] for r in missing["resources"]], ["a.md"])

    def test_pagination(self) -> None:
        page1 = self.call("resource.list", project_id=self.project_id, limit=2, offset=0)
        page2 = self.call("resource.list", project_id=self.project_id, limit=2, offset=2)
        self.assertEqual(page1["count"], 2)
        self.assertTrue(page1["has_more"])
        self.assertEqual(page2["count"], 1)
        self.assertFalse(page2["has_more"])
        seen = [r["path"] for r in page1["resources"]] + [r["path"] for r in page2["resources"]]
        self.assertEqual(sorted(seen), ["a.md", "b.csv", "c.py"])

    def test_compact_omits_heavy_payload_keeps_version_token(self) -> None:
        full = self.call("resource.list", project_id=self.project_id)["resources"][0]
        self.assertIn("current_version", full)  # heavy nested object present by default
        compact = self.call("resource.list", project_id=self.project_id, compact=True)
        first = compact["resources"][0]
        self.assertTrue(compact["compact"])
        self.assertNotIn("current_version", first)
        self.assertNotIn("associations", first)
        self.assertIn("version_token", first)  # cheap change-detection still available


if __name__ == "__main__":
    unittest.main()
