from __future__ import annotations

import unittest

from backend.domain.reflection_projection import (
    external_reflection_state,
    external_reflection_transition,
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

    def test_transition_ignores_non_dict_items(self) -> None:
        self.assertEqual(external_reflection_transition("ready"), "ready")


if __name__ == "__main__":
    unittest.main()
