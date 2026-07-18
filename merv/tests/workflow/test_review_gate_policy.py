from __future__ import annotations

import unittest

from backend.domain.review_gates import (
    REVIEW_GATE_EXEMPT_ROLES,
    REVIEW_GATE_ROLES,
    expected_review_gate_role,
    is_review_gate_exempt,
)
from backend.domain.reflection_gates import REFLECTION_GATE_TABLE
from backend.domain.workflow_gates import GATE_TABLE


def _review_roles_from_gate_tables() -> dict[tuple[str, str], str]:
    rows: dict[tuple[str, str], str] = {}
    for status, forward in GATE_TABLE.items():
        if forward.review is not None:
            rows[("experiment", status)] = forward.review.role
    for status, forward in REFLECTION_GATE_TABLE.items():
        if forward.review is not None:
            rows[("reflection", status)] = forward.review.role
    return rows


class ReviewGatePolicyTest(unittest.TestCase):
    def test_gate_roles_are_explicit_table_entries(self) -> None:
        self.assertEqual(
            REVIEW_GATE_ROLES,
            {
                ("experiment", "design_review"): "design_reviewer",
                ("experiment", "experiment_review"): "experiment_reviewer",
                ("reflection", "reflection_review"): "reflection_reviewer",
            },
        )

    def test_gate_roles_match_transition_review_requirements(self) -> None:
        self.assertEqual(REVIEW_GATE_ROLES, _review_roles_from_gate_tables())

    def test_expected_role_returns_none_outside_review_gates(self) -> None:
        self.assertEqual(
            expected_review_gate_role(
                target_type="experiment",
                target_status="design_review",
            ),
            "design_reviewer",
        )
        self.assertIsNone(
            expected_review_gate_role(
                target_type="experiment",
                target_status="running",
            )
        )

    def test_human_and_automated_checks_are_gate_exempt(self) -> None:
        self.assertEqual(REVIEW_GATE_EXEMPT_ROLES, {"human", "automated_check"})
        self.assertTrue(is_review_gate_exempt(role="human"))
        self.assertTrue(is_review_gate_exempt(role="automated_check"))
        self.assertFalse(is_review_gate_exempt(role="design_reviewer"))


if __name__ == "__main__":
    unittest.main()
