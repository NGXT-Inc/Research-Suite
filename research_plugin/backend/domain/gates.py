"""Shared declarative workflow gate contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleRequirement:
    """A current-attempt resource association the forward transition needs."""

    role: str
    # Enforcement: WorkflowError message when the association is absent.
    error: str
    # Enforcement: deep-lint hook run after the association exists
    # ("plan" | "report" | ""). The lint reads the SUBMITTED bytes pinned at
    # resource.associate - never the live file (fix-and-resubmit semantics).
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
