"""Shared declarative workflow gate contracts."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from ...kernel.utils import WorkflowError


@dataclass(frozen=True)
class RoleRequirement:
    """A current-attempt resource association the forward transition needs."""

    role: str
    # Enforcement: WorkflowError message when the association is absent.
    error: str
    # Enforcement: deep-lint hook run after the association exists
    # ("plan" | "report" | ""). The lint reads the SUBMITTED bytes pinned at
    # resource.register - never the live file (fix-and-resubmit semantics).
    validator: str = ""
    # Guidance while unmet: current_gate / next_action / allowed_actions /
    # missing_evidence entry / resource_guidance payload key.
    gate: str = ""
    action: str = ""
    allowed: tuple[str, ...] = ()
    missing: str = ""
    guidance_key: str = ""


@dataclass(frozen=True)
class ReviewRequirement:
    """A passing review the forward transition needs."""

    role: str
    skill: str
    action_name: str
    error: str
    pass_action: str


@dataclass(frozen=True)
class ForwardTransition:
    """The one forward transition out of a status, with its gate contract."""

    name: str
    to_status: str
    requires_prose: str = ""
    requirements: tuple[RoleRequirement, ...] = ()
    review: ReviewRequirement | None = None
    # Guidance once every requirement is met: "go transition".
    ready_gate: str = ""
    ready_action: str = ""
    ready_allowed: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequirementState:
    """Service-supplied current state for one declarative requirement."""

    role: str
    present: bool
    missing_error: str
    validation_error: str = ""


@dataclass(frozen=True)
class ReviewState:
    """Service-supplied current state for one review requirement."""

    satisfied: bool
    error: str
    blocked_reason: str = ""


def decide_gated_transition(
    *,
    subject: str,
    status: str,
    transition: str,
    terminal_statuses: set[str] | frozenset[str],
    direct_transitions: Mapping[str, str],
    forward: ForwardTransition | None,
    requirement_states: Sequence[RequirementState],
    review_state: ReviewState | None,
    allowed_transitions: Sequence[Mapping[str, Any]],
) -> str:
    """Pure transition decision shared by experiment and reflection services.

    Services collect state from SQL/pinned artifacts/review rows, then hand the
    plain booleans and errors here. This keeps orchestration out of the domain
    layer while making the workflow decision itself single-source.
    """
    if status in terminal_statuses:
        raise WorkflowError(
            f"{subject} is {status!r}; no transitions are allowed from a terminal state"
        )
    if transition in direct_transitions:
        return direct_transitions[transition]
    if forward is None or forward.name != transition:
        options = ", ".join(str(item["transition"]) for item in allowed_transitions)
        raise WorkflowError(
            f"transition {transition!r} is not allowed from {status!r}; "
            f"allowed from here: {options}"
        )
    for requirement in requirement_states:
        if not requirement.present:
            raise WorkflowError(requirement.missing_error)
        if requirement.validation_error:
            raise WorkflowError(requirement.validation_error)
    if review_state is not None and not review_state.satisfied:
        raise WorkflowError(review_state.blocked_reason or review_state.error)
    return forward.to_status
