"""Application presentation for semantic Research gate checklists."""

from __future__ import annotations

from typing import Any

from .guidance_catalog import (
    EXPERIMENT_REQUIREMENTS,
    REFLECTION_REQUIREMENTS,
    REVIEWS,
)

Record = dict[str, Any]


def present_gate_checklist(checklist: Record) -> Record:
    """Add agent actions and skills without changing the wire-field order."""

    if not checklist or "items" not in checklist:
        return checklist
    return {
        **checklist,
        "items": [_present_item(item) for item in checklist.get("items", [])],
    }


def _present_item(item: Record) -> Record:
    role = str(item.get("role") or "")
    review = REVIEWS.get(role)
    requirement = EXPERIMENT_REQUIREMENTS.get(role) or REFLECTION_REQUIREMENTS.get(
        role
    )
    if review is None and requirement is None:
        return dict(item)
    action = (
        review.pass_action
        if review is not None and item.get("satisfied")
        else f"launch_{review.action_name}er"
        if review is not None
        else requirement.action
    )
    presentation = {"action": action}
    if review is not None:
        presentation["skill"] = review.skill
    result = {}
    for field, value in item.items():
        if field not in presentation:
            result[field] = value
        if field == "gate":
            result.update(presentation)
    if "action" not in result:
        result.update(presentation)
    return result


__all__ = ["present_gate_checklist"]
