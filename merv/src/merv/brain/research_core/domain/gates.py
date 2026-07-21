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
    # resource.register - never the live file (fix-and-resubmit semantics).
    validator: str = ""
    # Stable semantic facts used by enforcement and the public checklist.
    gate: str = ""
    missing: str = ""
    label: str = ""


@dataclass(frozen=True)
class ReviewRequirement:
    """A passing review the forward transition needs."""

    role: str
    error: str
    blocker_code: str
    label: str = ""


@dataclass(frozen=True)
class ForwardTransition:
    """The one forward transition out of a status, with its gate contract."""

    name: str
    to_status: str
    requires_prose: str = ""
    requirements: tuple[RoleRequirement, ...] = ()
    review: ReviewRequirement | None = None
