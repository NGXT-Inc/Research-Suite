"""External reflection naming for the project-reflection tool surface."""

from __future__ import annotations

from typing import Any


def external_reflection_status(status: Any) -> Any:
    return "reflection_review" if status == "synthesis_review" else status


def external_reflection_transition(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    output = dict(item)
    if output.get("transition") == "submit_synthesis":
        output["transition"] = "submit_reflection_artifacts"
    if output.get("leads_to") == "synthesis_review":
        output["leads_to"] = "reflection_review"
    for field in ("requires", "description"):
        if isinstance(output.get(field), str):
            output[field] = output[field].replace(
                "synthesis_reviewer",
                "reflection_reviewer",
            ).replace(
                "submit_synthesis",
                "submit_reflection_artifacts",
            )
    return output


def external_reflection_checklist(checklist: Any) -> Any:
    if not isinstance(checklist, dict):
        return checklist
    output = external_reflection_transition(checklist)
    if "status" in output:
        output["status"] = external_reflection_status(output.get("status"))
    items = []
    for item in output.get("items", []):
        if not isinstance(item, dict):
            items.append(item)
            continue
        projected = dict(item)
        if "gate" in projected:
            projected["gate"] = external_reflection_status(projected.get("gate"))
        if isinstance(projected.get("action"), str):
            projected["action"] = projected["action"].replace(
                "submit_synthesis",
                "submit_reflection_artifacts",
            ).replace(
                "synthesis_reviewer",
                "reflection_reviewer",
            )
        items.append(projected)
    output["items"] = items
    return output


def external_reflection_state(state: dict[str, Any]) -> dict[str, Any]:
    output = dict(state)
    if "status" in output:
        output["status"] = external_reflection_status(output.get("status"))
    if "allowed_transitions" in output:
        output["allowed_transitions"] = [
            external_reflection_transition(item)
            for item in output.get("allowed_transitions", [])
        ]
    if "gate_checklist" in output:
        output["gate_checklist"] = external_reflection_checklist(
            output.get("gate_checklist")
        )
    return output


def internal_synthesis_transition(transition: Any) -> Any:
    """External 'submit_reflection_artifacts' tool name -> internal 'submit_synthesis'; pass through all else."""
    return (
        "submit_synthesis"
        if transition == "submit_reflection_artifacts"
        else transition
    )
