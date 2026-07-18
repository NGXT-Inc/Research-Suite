"""Permission and policy checks for v0.0001."""

from __future__ import annotations

from ..artifacts.roles import (
    LEGACY_PROJECT_GRAPH_ROLE,
    LEGACY_PROPOSALS_ROLE,
    LEGACY_REFLECTION_DOC_ROLE,
    LEGACY_REFLECTION_LENS_DOC_ROLE,
    LEGACY_RESOURCE_ROLES,
    PROJECT_GRAPH_ROLE,
    REFLECTION_LENS_DOC_ROLE,
    RESOURCE_ROLES,
    RESOURCE_TARGET_TYPES,
)
from ..domain.vocabulary import REVIEW_ROLES, REVIEW_VERDICTS
from ..utils import PermissionDeniedError, ValidationError


class PermissionService:
    """Small policy layer, intentionally separate from workflow and persistence."""

    def validate_review_role(self, *, role: str) -> None:
        if role not in REVIEW_ROLES:
            raise ValidationError(f"unknown review role: {role}")

    def validate_review_verdict(self, *, verdict: str) -> None:
        if verdict not in REVIEW_VERDICTS:
            raise ValidationError(f"unknown review verdict: {verdict}")

    def validate_resource_association(self, *, target_type: str, role: str) -> None:
        if target_type not in RESOURCE_TARGET_TYPES:
            allowed = sorted(RESOURCE_TARGET_TYPES)
            raise ValidationError(
                f"unknown resource target type: {target_type}. Allowed target types: {', '.join(allowed)}",
                details={"allowed_target_types": allowed},
            )
        if role in LEGACY_RESOURCE_ROLES:
            replacements = {
                LEGACY_REFLECTION_LENS_DOC_ROLE: REFLECTION_LENS_DOC_ROLE,
                LEGACY_REFLECTION_DOC_ROLE: "reflection_doc",
                LEGACY_PROPOSALS_ROLE: "change_spec",
            }
            replacement = replacements[role]
            raise ValidationError(
                f"legacy resource role {role!r} is read-only for old records; use {replacement!r}",
                details={
                    "legacy_role": role,
                    "replacement_role": replacement,
                },
            )
        if target_type == "reflection" and role == LEGACY_PROJECT_GRAPH_ROLE:
            raise ValidationError(
                "use role 'project_graph' for reflection-wave project graphs; "
                "role 'graph' is only for experiment logic graphs",
                details={
                    "legacy_role": LEGACY_PROJECT_GRAPH_ROLE,
                    "replacement_role": PROJECT_GRAPH_ROLE,
                },
            )
        if role not in RESOURCE_ROLES:
            allowed = sorted(RESOURCE_ROLES)
            raise ValidationError(
                f"unknown resource role: {role}. Allowed roles: {', '.join(allowed)}",
                details={"allowed_resource_roles": allowed, "recommended_result_role": "result"},
            )

    def reject_reviewer_mutation(self, *, tool_name: str, review_session_id: str | None) -> None:
        if review_session_id and tool_name != "review.submit":
            raise PermissionDeniedError("review sessions are read-only except review.submit")
