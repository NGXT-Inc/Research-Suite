"""Declarative mapping from workflow gates to reviewer roles."""

from __future__ import annotations

REVIEW_GATE_EXEMPT_ROLES = frozenset({"human", "automated_check"})

REVIEW_GATE_ROLES: dict[tuple[str, str], str] = {
    ("experiment", "design_review"): "design_reviewer",
    ("experiment", "experiment_review"): "experiment_reviewer",
    ("reflection", "reflection_review"): "reflection_reviewer",
}


def is_review_gate_exempt(*, role: str) -> bool:
    return role in REVIEW_GATE_EXEMPT_ROLES


def expected_review_gate_role(*, target_type: str, target_status: str) -> str | None:
    return REVIEW_GATE_ROLES.get((target_type, target_status))
