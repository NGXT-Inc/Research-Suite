"""Declarative review rejection return routing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewReturnRule:
    allowed: tuple[str, ...]
    default: str
    invalid_message: str
    explicit_required: bool = False
    required_message: str = ""
    forbidden: tuple[tuple[str, str], ...] = ()


PASS_RETURN_TO_ERROR = (
    "return_to only applies when the verdict is needs_changes or fail"
)

EXPERIMENT_RETURN_TO_ERROR = "return_to must be 'planned' or 'running'"
REFLECTION_RETURN_TO_ERROR = (
    "return_to must be 'reflecting' or 'synthesizing' for reflection reviews"
)

EXPERIMENT_REVIEWER_RETURN_TO_REQUIRED = (
    "experiment-attempt-review rejections must set return_to: 'planned' if the "
    "results show the plan itself is flawed, or 'running' if the plan "
    "stands but execution or the conclusion is flawed"
)
DESIGN_REVIEW_RUNNING_ERROR = (
    "experiment-design-review rejections cannot return_to 'running'; a flawed plan "
    "goes back to 'planned'"
)
REFLECTION_REVIEWER_RETURN_TO_REQUIRED = (
    "project-reflection-review rejections must set return_to: 'reflecting' "
    "to re-launch the reflection fan-out (the reflections "
    "themselves are inadequate), or 'synthesizing' if the "
    "reflections stand but the reflection artifacts must be revised"
)

REVIEW_RETURN_RULES: dict[tuple[str, str], ReviewReturnRule] = {
    ("experiment", "*"): ReviewReturnRule(
        allowed=("", "planned", "running"),
        default="planned",
        invalid_message=EXPERIMENT_RETURN_TO_ERROR,
    ),
    ("experiment", "experiment_reviewer"): ReviewReturnRule(
        allowed=("", "planned", "running"),
        default="planned",
        invalid_message=EXPERIMENT_RETURN_TO_ERROR,
        explicit_required=True,
        required_message=EXPERIMENT_REVIEWER_RETURN_TO_REQUIRED,
    ),
    ("experiment", "design_reviewer"): ReviewReturnRule(
        allowed=("", "planned", "running"),
        default="planned",
        invalid_message=EXPERIMENT_RETURN_TO_ERROR,
        forbidden=(("running", DESIGN_REVIEW_RUNNING_ERROR),),
    ),
    ("reflection", "*"): ReviewReturnRule(
        allowed=("", "reflecting", "synthesizing"),
        default="synthesizing",
        invalid_message=REFLECTION_RETURN_TO_ERROR,
    ),
    ("reflection", "reflection_reviewer"): ReviewReturnRule(
        allowed=("", "reflecting", "synthesizing"),
        default="synthesizing",
        invalid_message=REFLECTION_RETURN_TO_ERROR,
        explicit_required=True,
        required_message=REFLECTION_REVIEWER_RETURN_TO_REQUIRED,
    ),
}


def resolve_review_return(
    *, target_type: str, role: str, verdict: str, return_to: str
) -> str:
    """Resolve a submitted review return target or raise ``ValueError``."""
    value = (return_to or "").strip()
    rule = REVIEW_RETURN_RULES.get((target_type, role)) or REVIEW_RETURN_RULES.get(
        (target_type, "*")
    )
    if rule is None:
        raise ValueError(f"unknown review target type: {target_type}")
    if value not in rule.allowed:
        raise ValueError(rule.invalid_message)
    if verdict == "pass":
        if value:
            raise ValueError(PASS_RETURN_TO_ERROR)
        return ""
    for forbidden, message in rule.forbidden:
        if value == forbidden:
            raise ValueError(message)
    if rule.explicit_required and not value:
        raise ValueError(rule.required_message)
    return value or rule.default


def revision_context_for_review_return(
    *,
    target_type: str,
    role: str,
    verdict: str,
    notes: str,
    findings: list[dict[str, object]],
    return_to: str = "",
) -> str:
    finding_text = "; ".join(
        str(item.get("issue", "")) for item in findings if item.get("issue")
    )
    pieces = [f"{role} returned {verdict}"]
    if target_type == "experiment" and return_to == "running":
        pieces.append(
            "Sent back to running: the approved plan stands; fix execution "
            "and/or the conclusion, then retain/associate results and resubmit"
        )
    if target_type == "reflection":
        if return_to == "reflecting":
            pieces.append(
                "Sent back to reflecting: re-launch the reflection fan-out — "
                "every roster lens must submit a fresh reflection for the "
                "new attempt"
            )
        else:
            pieces.append(
                "Sent back to synthesizing: the reflections stand; revise "
                "the reflection artifacts (project graph, reflection doc, "
                "and/or change spec) and resubmit"
            )
    if notes:
        pieces.append(notes)
    if finding_text:
        pieces.append(f"Findings: {finding_text}")
    if target_type == "reflection":
        pieces.append(
            "Consider revising the project graph, reflection doc, and/or "
            "change spec where this review changes the project's story; "
            "the 16-node graph budget still applies"
        )
    else:
        pieces.append(
            "Consider updating the experiment's logic graph (role 'graph') if "
            "this review changes the experiment's story; the 16-node budget "
            "still applies"
        )
    return " | ".join(pieces)
