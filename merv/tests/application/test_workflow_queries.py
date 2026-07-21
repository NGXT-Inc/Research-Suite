from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests.support.brain import TestBrain


class WorkflowQueryIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=root,
            db_path=root / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.app.call_tool(
            "project", {"action": "create", "name": "Workflow query"}
        )["id"]
        self.experiment_ids = [
            self.app.call_tool(
                "experiment.create",
                {
                    "project_id": self.project_id,
                    "name": f"read-{index}",
                    "intent": f"Read experiment {index} once.",
                },
            )["id"]
            for index in range(2)
        ]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def test_project_dashboard_hydrates_each_experiment_once(self) -> None:
        original = self.app.experiments.get_state
        wrapped = Mock(wraps=original)
        self.app.experiments.get_state = wrapped
        try:
            result = self.app.project_dashboard_query(project_id=self.project_id)
        finally:
            self.app.experiments.get_state = original

        self.assertCountEqual(
            [experiment["id"] for experiment in result["experiments"]],
            self.experiment_ids,
        )
        hydrated = [str(call.kwargs["experiment_id"]) for call in wrapped.call_args_list]
        self.assertCountEqual(hydrated, self.experiment_ids)

    def test_scoped_workflow_hydrates_only_the_selected_experiment(self) -> None:
        original = self.app.experiments.get_state
        wrapped = Mock(wraps=original)
        self.app.experiments.get_state = wrapped
        try:
            result = self.app.workflow.status_and_next(
                project_id=self.project_id,
                experiment_id=self.experiment_ids[0],
            )
        finally:
            self.app.experiments.get_state = original

        self.assertEqual(result["experiment"]["id"], self.experiment_ids[0])
        self.assertEqual(
            [str(call.kwargs["experiment_id"]) for call in wrapped.call_args_list],
            [self.experiment_ids[0]],
        )


if __name__ == "__main__":
    unittest.main()
