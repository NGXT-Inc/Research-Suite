from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.domain.experiment_policy import ACTIVE_EXPERIMENT_CAP
from backend.domain.reflection_policy import REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD
from backend.utils import WorkflowError


class ReflectionExperimentWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.call("project", action="create", name="Experiment Writer Test")[
            "id"
        ]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def test_reflection_writer_preserves_payload_and_bypasses_reflection_debt(self) -> None:
        claim = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="A reflection-created wave can test this claim.",
        )
        terminal_experiments = [
            self.call(
                "experiment.create",
                project_id=self.project_id,
                name=f"finished-{index}",
                intent="Seed terminal reflection debt.",
            )["id"]
            for index in range(REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD)
        ]
        for experiment_id in terminal_experiments:
            self.call(
                "experiment.transition",
                project_id=self.project_id,
                experiment_id=experiment_id,
                transition="abandon",
            )
        with self.assertRaises(WorkflowError):
            self.call(
                "experiment.create",
                project_id=self.project_id,
                name="blocked-create",
                intent="Normal creation should require reflection.",
            )

        with self.app.store.transaction() as conn:
            experiment_id = self.app.experiments.create_from_reflection(
                conn=conn,
                project_id=self.project_id,
                reflection_id="syn_contract",
                name="reflection-wave",
                intent="Created from an approved reflection change spec.",
                claim_ids=[claim["id"]],
                proposal_key="wave_a",
                parallelism="Independent axis.",
            )

        state = self.call(
            "experiment.get_state",
            project_id=self.project_id,
            experiment_id=experiment_id,
        )
        self.assertEqual(state["name"], "reflection-wave")
        self.assertEqual(state["status"], "planned")
        self.assertEqual([item["id"] for item in state["tested_claims"]], [claim["id"]])

        events = self.app.store.recent_events(project_id=self.project_id, limit=20)[
            "events"
        ]
        created = next(
            event
            for event in events
            if event["type"] == "experiment.created"
            and event["target_id"] == experiment_id
        )
        self.assertEqual(
            created["payload"],
            {
                "name": "reflection-wave",
                "intent": "Created from an approved reflection change spec.",
                "source_reflection_id": "syn_contract",
                "proposal_key": "wave_a",
                "parallelism": "Independent axis.",
            },
        )

    def test_reflection_writer_enforces_active_experiment_cap(self) -> None:
        claim = self.call(
            "claim.create",
            project_id=self.project_id,
            statement="A reflection-created experiment must fit the active cap.",
        )
        for index in range(ACTIVE_EXPERIMENT_CAP):
            self.call(
                "experiment.create",
                project_id=self.project_id,
                name=f"active-{index}",
                intent="Keep this experiment active.",
            )

        with self.app.store.transaction() as conn:
            with self.assertRaises(WorkflowError) as ctx:
                self.app.experiments.create_from_reflection(
                    conn=conn,
                    project_id=self.project_id,
                    reflection_id="syn_contract",
                    name="reflection-over-cap",
                    intent="Created from an approved reflection change spec.",
                    claim_ids=[claim["id"]],
                    proposal_key="wave_a",
                    parallelism="Independent axis.",
                )
        self.assertIn("finish one before creating another", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
