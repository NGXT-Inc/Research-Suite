"""Permission and policy checks for v0.0001."""

from __future__ import annotations

from ..utils import PermissionDeniedError, ValidationError


REVIEW_ROLES = {
    "design_reviewer",
    "experiment_reviewer",
    "synthesis_reviewer",
    "human",
    "automated_check",
}
REVIEW_VERDICTS = {"pass", "needs_changes", "fail"}
RESOURCE_TARGET_TYPES = {"experiment", "synthesis", "claim", "review", "attempt"}
RESOURCE_ROLES = {
    "plan", "input", "code", "config", "result", "report", "graph",
    "reflection", "proposals", "note", "model", "other",
}

# Gated roles: the artifacts workflow gates lint. Associating one of these
# captures the file's bytes into the blob store (size-capped), pinning the
# association to immutable content (docs/CLOUD_BACKEND_MIGRATION_PLAN.md
# decision 6). The report/graph caps mirror artifacts.MAX_REPORT_BYTES and
# graph_lint.MAX_GRAPH_BYTES (alignment pinned by a structure test); plan/
# proposals/reflection previously had no cap at all.
GATED_ROLE_BYTE_CAPS: dict[str, int] = {
    "plan": 16_000,
    "report": 16_000,
    "graph": 16_000,
    "proposals": 16_000,
    "reflection": 16_000,
}
GATED_ROLES = frozenset(GATED_ROLE_BYTE_CAPS)


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
        if role not in RESOURCE_ROLES:
            allowed = sorted(RESOURCE_ROLES)
            raise ValidationError(
                f"unknown resource role: {role}. Allowed roles: {', '.join(allowed)}",
                details={"allowed_resource_roles": allowed, "recommended_result_role": "result"},
            )

    def reject_reviewer_mutation(self, *, tool_name: str, review_session_id: str | None) -> None:
        if review_session_id and tool_name != "review.submit":
            raise PermissionDeniedError("review sessions are read-only except review.submit")
