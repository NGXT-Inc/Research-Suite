"""Pure reviewer handoff payload construction."""

from __future__ import annotations

from typing import Any

from ..artifacts.roles import external_reflection_target_type


def reviewer_handoff_payload(
    *,
    role: str,
    target_type: str,
    target_id: str,
    review_request_id: str = "",
    reviewer_capability: str = "",
) -> dict[str, Any]:
    skill = {
        "design_reviewer": "experiment-design-review",
        "experiment_reviewer": "experiment-attempt-review",
        "reflection_reviewer": "project-reflection-review",
    }.get(role, "")
    external_type = external_reflection_target_type(target_type)
    handoff: dict[str, Any] = {
        "role": role,
        "skill": skill,
        "target_type": external_type,
        "target_id": target_id,
        "read_only": True,
        "start_tool": "review.start",
        "submit_tool": "review.submit",
    }
    if review_request_id and reviewer_capability and skill:
        handoff["spawn_prompt"] = (
            f"You are the {role} for {external_type} {target_id}. "
            f"Follow the {skill} skill. Begin by calling review.start with "
            f"review_request_id={review_request_id}, "
            f"reviewer_capability={reviewer_capability}, and your own "
            "session identity as caller_session_id (required; never the "
            "producer's). You are read-only: your sole permitted mutation "
            "is review.submit."
        )
    return handoff
