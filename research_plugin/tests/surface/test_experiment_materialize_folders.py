from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.execution.backends.fake import FakeSandboxBackend


class ExperimentMaterializeFoldersToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.project_id = self.app.call_tool(
            "project",
            {"action": "create", "name": "Folder Sync"},
        )["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def test_materializes_planned_experiment_folders_on_demand(self) -> None:
        first = self.app.call_tool(
            "experiment.create",
            {
                "project_id": self.project_id,
                "name": "alpha",
                "intent": "First folder.",
            },
        )
        second = self.app.call_tool(
            "experiment.create",
            {
                "project_id": self.project_id,
                "name": "beta",
                "intent": "Second folder.",
            },
        )
        self.assertFalse((self.repo / "experiments" / "alpha").exists())
        self.assertFalse((self.repo / "experiments" / "beta").exists())

        result = self.app.call_tool(
            "experiment.materialize_folders",
            {"project_id": self.project_id},
        )

        self.assertEqual(result["count"], 2)
        self.assertEqual(
            {item["experiment_id"] for item in result["folders"]},
            {first["id"], second["id"]},
        )
        self.assertTrue((self.repo / "experiments" / "alpha").is_dir())
        self.assertTrue((self.repo / "experiments" / "beta").is_dir())
        self.assertTrue(all(item["created"] for item in result["folders"]))

        again = self.app.call_tool(
            "experiment.materialize_folders",
            {"project_id": self.project_id},
        )
        self.assertEqual(again["count"], 2)
        self.assertFalse(any(item["created"] for item in again["folders"]))

    def test_materializes_one_experiment_regardless_of_status_filter(self) -> None:
        experiment = self.app.call_tool(
            "experiment.create",
            {
                "project_id": self.project_id,
                "name": "single",
                "intent": "One folder.",
            },
        )

        result = self.app.call_tool(
            "experiment.materialize_folders",
            {
                "project_id": self.project_id,
                "experiment_id": experiment["id"],
                "status": "running",
            },
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["folders"][0]["folder"], "experiments/single/")
        self.assertTrue((self.repo / "experiments" / "single").is_dir())


if __name__ == "__main__":
    unittest.main()
