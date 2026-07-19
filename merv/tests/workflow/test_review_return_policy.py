from __future__ import annotations

import unittest

from merv.brain.research_core.domain.review_returns import (
    REVIEW_RETURN_RULES,
    resolve_review_return,
)


class ReviewReturnPolicyTest(unittest.TestCase):
    def test_pass_verdict_rejects_return_to(self) -> None:
        with self.assertRaisesRegex(ValueError, "return_to only applies"):
            resolve_review_return(
                target_type="experiment",
                role="human",
                verdict="pass",
                return_to="planned",
            )

    def test_experiment_reviewer_must_choose_return_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "experiment-attempt-review"):
            resolve_review_return(
                target_type="experiment",
                role="experiment_reviewer",
                verdict="needs_changes",
                return_to="",
            )
        self.assertEqual(
            resolve_review_return(
                target_type="experiment",
                role="experiment_reviewer",
                verdict="needs_changes",
                return_to="running",
            ),
            "running",
        )

    def test_design_reviewer_defaults_to_planned_and_cannot_run(self) -> None:
        self.assertEqual(
            resolve_review_return(
                target_type="experiment",
                role="design_reviewer",
                verdict="fail",
                return_to="",
            ),
            "planned",
        )
        with self.assertRaisesRegex(ValueError, "cannot return_to 'running'"):
            resolve_review_return(
                target_type="experiment",
                role="design_reviewer",
                verdict="needs_changes",
                return_to="running",
            )

    def test_reflection_reviewer_must_choose_return_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "project-reflection-review"):
            resolve_review_return(
                target_type="reflection",
                role="reflection_reviewer",
                verdict="needs_changes",
                return_to="",
            )
        self.assertEqual(
            resolve_review_return(
                target_type="reflection",
                role="reflection_reviewer",
                verdict="needs_changes",
                return_to="reflecting",
            ),
            "reflecting",
        )

    def test_default_rules_remain_explicit_table_entries(self) -> None:
        self.assertEqual(
            set(REVIEW_RETURN_RULES),
            {
                ("experiment", "*"),
                ("experiment", "experiment_reviewer"),
                ("experiment", "design_reviewer"),
                ("reflection", "*"),
                ("reflection", "reflection_reviewer"),
            },
        )

    def test_unknown_target_type_is_validation_failure(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown review target type"):
            resolve_review_return(
                target_type="claim",
                role="human",
                verdict="needs_changes",
                return_to="",
            )


if __name__ == "__main__":
    unittest.main()
