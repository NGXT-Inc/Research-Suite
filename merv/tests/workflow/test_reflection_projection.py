from __future__ import annotations

import unittest

from backend.artifacts.roles import (
    external_reflection_target_type,
    internal_synthesis_target_type,
)
from backend.domain.reflection_projection import (
    external_reflection_state,
    external_reflection_transition,
    internal_synthesis_transition,
)


class ReflectionProjectionTest(unittest.TestCase):
    def test_state_preserves_absent_status(self) -> None:
        self.assertEqual(external_reflection_state({"id": "syn_1"}), {"id": "syn_1"})

    def test_state_rewrites_review_status_and_transitions(self) -> None:
        state = {
            "status": "synthesis_review",
            "allowed_transitions": [
                {
                    "transition": "submit_synthesis",
                    "leads_to": "synthesis_review",
                    "requires": "synthesis_reviewer approves submit_synthesis",
                }
            ],
        }

        self.assertEqual(
            external_reflection_state(state),
            {
                "status": "reflection_review",
                "allowed_transitions": [
                    {
                        "transition": "submit_reflection_artifacts",
                        "leads_to": "reflection_review",
                        "requires": (
                            "reflection_reviewer approves "
                            "submit_reflection_artifacts"
                        ),
                    }
                ],
            },
        )

    def test_state_rewrites_gate_checklist(self) -> None:
        state = {
            "status": "synthesis_review",
            "gate_checklist": {
                "status": "synthesis_review",
                "transition": "submit_synthesis",
                "leads_to": "synthesis_review",
                "ready": False,
                "items": [
                    {
                        "id": "resource:project_graph",
                        "gate": "synthesis_review",
                        "action": "submit_synthesis",
                    }
                ],
            },
        }

        self.assertEqual(
            external_reflection_state(state)["gate_checklist"],
            {
                "status": "reflection_review",
                "transition": "submit_reflection_artifacts",
                "leads_to": "reflection_review",
                "ready": False,
                "items": [
                    {
                        "id": "resource:project_graph",
                        "gate": "reflection_review",
                        "action": "submit_reflection_artifacts",
                    }
                ],
            },
        )

    def test_transition_ignores_non_dict_items(self) -> None:
        self.assertEqual(external_reflection_transition("ready"), "ready")

    def test_external_reflection_target_type(self) -> None:
        self.assertEqual(external_reflection_target_type("synthesis"), "reflection")
        self.assertEqual(external_reflection_target_type("experiment"), "experiment")
        self.assertIsNone(external_reflection_target_type(None))

    def test_internal_synthesis_target_type(self) -> None:
        self.assertEqual(internal_synthesis_target_type("reflection"), "synthesis")
        self.assertEqual(internal_synthesis_target_type("experiment"), "experiment")
        self.assertIsNone(internal_synthesis_target_type(None))

    def test_internal_synthesis_transition(self) -> None:
        self.assertEqual(
            internal_synthesis_transition("submit_reflection_artifacts"),
            "submit_synthesis",
        )
        self.assertEqual(internal_synthesis_transition("reject"), "reject")


if __name__ == "__main__":
    unittest.main()
