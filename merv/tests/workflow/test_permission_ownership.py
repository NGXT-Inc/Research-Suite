from __future__ import annotations

import unittest

from merv.brain.artifacts.association_policy import validate_resource_association
from merv.brain.kernel.utils import PermissionDeniedError, ValidationError
from merv.brain.research_core.domain.review_validation import (
    validate_review_role,
    validate_review_verdict,
)
from merv.brain.surface.permissions import PermissionService


class OwnedPermissionPolicyTest(unittest.TestCase):
    def test_research_validates_review_vocabulary(self) -> None:
        validate_review_role(role="experiment_reviewer")
        validate_review_verdict(verdict="needs_changes")
        with self.assertRaisesRegex(ValidationError, "unknown review role: visitor"):
            validate_review_role(role="visitor")
        with self.assertRaisesRegex(ValidationError, "unknown review verdict: maybe"):
            validate_review_verdict(verdict="maybe")

    def test_artifacts_validates_association_vocabulary(self) -> None:
        validate_resource_association(target_type="experiment", role="plan")
        with self.assertRaises(ValidationError) as target_error:
            validate_resource_association(target_type="project", role="plan")
        self.assertIn("experiment", target_error.exception.details["allowed_target_types"])
        with self.assertRaises(ValidationError) as legacy_error:
            validate_resource_association(target_type="reflection", role="synthesis_doc")
        self.assertEqual(legacy_error.exception.details["replacement_role"], "reflection_doc")
        with self.assertRaises(ValidationError) as graph_error:
            validate_resource_association(target_type="reflection", role="graph")
        self.assertEqual(graph_error.exception.details["replacement_role"], "project_graph")

    def test_surface_permission_is_only_tool_authorization(self) -> None:
        policy = PermissionService()
        policy.reject_reviewer_mutation(
            tool_name="review.submit", review_session_id="rvs_1"
        )
        with self.assertRaises(PermissionDeniedError):
            policy.reject_reviewer_mutation(
                tool_name="claim.create", review_session_id="rvs_1"
            )


if __name__ == "__main__":
    unittest.main()
