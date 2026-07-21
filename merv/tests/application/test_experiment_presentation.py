from __future__ import annotations

import unittest
from copy import deepcopy

from merv.brain.application.experiments.presentation import (
    rich_experiment_state,
    slim_experiment_state,
)


class ExperimentPresentationTest(unittest.TestCase):
    def test_rich_projection_replaces_storage_at_its_historical_position(self) -> None:
        state = {
            "id": "exp_1",
            "current_attempt_resources": [],
            "mlflow_run": None,
            "reviews": [],
        }
        original = deepcopy(state)
        objects = [{"id": "so_1", "name": "model.bin"}]

        result = rich_experiment_state(state, storage_objects=objects)

        self.assertEqual(
            list(result),
            [
                "id",
                "current_attempt_resources",
                "storage_objects",
                "mlflow_run",
                "reviews",
            ],
        )
        self.assertEqual(result["storage_objects"], objects)
        self.assertEqual(state, original)

    def test_agent_projection_preserves_exact_shape_and_prior_order(self) -> None:
        state = {
            "id": "exp_1",
            "name": "projection",
            "status": "running",
            "attempt_index": 2,
            "intent": "Keep substance.",
            "conclusion": "",
            "revision_context": "retry",
            "created_at": "created",
            "updated_at": "updated",
            "allowed_transitions": [{"transition": "submit_results"}],
            "gate_checklist": {"result": {"satisfied": False}},
            "mlflow_run": {"run_id": "run_1"},
            "claim_update_suggestions": [],
            "tested_claims": [
                {
                    "id": "claim_1",
                    "statement": "It works",
                    "confidence": "high",
                    "status": "active",
                    "scope": "project",
                    "private": "drop",
                }
            ],
            "resources": [
                {
                    "id": "res_old",
                    "association_role": "report",
                    "association_attempt_index": 1,
                    "path": "old.md",
                    "kind": "report",
                },
                {
                    "id": "res_current",
                    "association_role": "report",
                    "association_attempt_index": 2,
                    "path": "report.md",
                    "kind": "report",
                    "size_bytes": 12,
                    "missing": False,
                    "title": "Report",
                },
            ],
            "reviews": [
                {
                    "id": "rev_1",
                    "role": "experiment_reviewer",
                    "verdict": "pass",
                    "created_at": "reviewed",
                    "synopsis": "sound",
                    "findings": [],
                    "notes": "ok",
                    "evidence": {"exit_code": 0},
                    "target_snapshot_id": "drop",
                }
            ],
        }
        objects = [
            {
                "id": "so_1",
                "name": "model.bin",
                "version": 2,
                "kind": "model",
                "content_sha256": "a" * 64,
                "size_bytes": 4,
                "content_type": "application/octet-stream",
                "status": "available",
                "expires_at": None,
                "producing_run": "run_1",
                "source_uri": "",
                "notes": "kept",
                "created_at": "drop",
            }
        ]

        result = slim_experiment_state(state, storage_objects=objects)

        self.assertEqual(result["current_attempt_resources"], [
            {
                "id": "res_current",
                "association_role": "report",
                "path": "report.md",
                "kind": "report",
                "size_bytes": 12,
                "missing": False,
                "title": "Report",
            }
        ])
        self.assertEqual(result["prior_attempt_resources"], [
            {
                "id": "res_old",
                "association_role": "report",
                "path": "old.md",
                "association_attempt_index": 1,
            }
        ])
        self.assertEqual(set(result["storage_objects"][0]), {
            "id", "name", "version", "kind", "content_sha256", "size_bytes",
            "content_type", "status", "expires_at", "producing_run", "source_uri",
            "notes",
        })
        self.assertNotIn("target_snapshot_id", result["reviews"][0])

    def test_explicit_empty_current_resources_does_not_fall_back(self) -> None:
        state = {
            "id": "exp_1",
            "attempt_index": 1,
            "resources": [
                {
                    "id": "res_1",
                    "association_attempt_index": 1,
                    "association_role": "plan",
                }
            ],
            "current_attempt_resources": [],
        }

        result = slim_experiment_state(state, storage_objects=[])

        self.assertEqual(result["current_attempt_resources"], [])
        self.assertNotIn("prior_attempt_resources", result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
