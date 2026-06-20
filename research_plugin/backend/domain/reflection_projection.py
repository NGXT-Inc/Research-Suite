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


def external_reflection_state(state: dict[str, Any]) -> dict[str, Any]:
    output = dict(state)
    if "status" in output:
        output["status"] = external_reflection_status(output.get("status"))
    if "allowed_transitions" in output:
        output["allowed_transitions"] = [
            external_reflection_transition(item)
            for item in output.get("allowed_transitions", [])
        ]
    return output
