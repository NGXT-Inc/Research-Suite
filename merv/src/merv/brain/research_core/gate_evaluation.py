"""Immutable Research workflow gate facts and their legacy projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from .domain.gates import RoleRequirement
from ..kernel.utils import WorkflowError


JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)
GateItem: TypeAlias = dict[str, JSONValue]
EvaluationStatus = Literal[
    "missing", "present", "valid", "invalid", "pending", "requested", "started", "passed"
]


@dataclass(frozen=True, slots=True)
class RequirementEvaluation:
    role: str
    status: EvaluationStatus
    blocker_code: str
    enforcement_error: str
    problems: tuple[str, ...]
    items: tuple[GateItem, ...]

    @property
    def satisfied(self) -> bool:
        return not self.enforcement_error

    @property
    def explanation(self) -> str:
        return self.enforcement_error if not self.satisfied else ""


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    subject: str
    status: str
    transition: str | None
    leads_to: str | None
    terminal: bool
    requirements: tuple[RequirementEvaluation, ...]
    review: RequirementEvaluation | None
    legal_transitions: tuple[dict[str, str], ...]

    @property
    def blocker(self) -> RequirementEvaluation | None:
        return next(
            (item for item in (*self.requirements, self.review) if item and not item.satisfied),
            None,
        )

    @property
    def blocker_code(self) -> str:
        return "" if self.blocker is None else self.blocker.blocker_code

    @property
    def explanation(self) -> str:
        return "" if self.blocker is None else self.blocker.explanation

    @property
    def ready(self) -> bool:
        return self.terminal if self.transition is None else self.blocker is None

    def checklist(self) -> dict[str, JSONValue]:
        items = [dict(item) for gate in self.requirements for item in gate.items]
        if self.review is not None:
            items.extend(dict(item) for item in self.review.items)
        return {
            "status": self.status,
            "transition": self.transition,
            "leads_to": self.leads_to,
            "ready": self.ready,
            "items": items,
        }

    def require_transition(self, transition: str) -> str:
        if self.terminal:
            raise WorkflowError(
                f"{self.subject} is {self.status!r}; no transitions are allowed from a terminal state"
            )
        selected = next(
            (
                item
                for item in self.legal_transitions
                if item["transition"] == transition
            ),
            None,
        )
        if selected is None:
            options = ", ".join(item["transition"] for item in self.legal_transitions)
            raise WorkflowError(
                f"transition {transition!r} is not allowed from {self.status!r}; "
                f"allowed from here: {options}"
            )
        if transition != self.transition:
            return selected["leads_to"]
        for requirement in self.requirements:
            if not requirement.satisfied:
                raise WorkflowError(requirement.enforcement_error)
        if self.review is not None and not self.review.satisfied:
            raise WorkflowError(self.review.enforcement_error)
        return selected["leads_to"]


def evaluate_resource_requirement(
    requirement: RoleRequirement,
    *,
    present: bool,
    problems: tuple[str, ...] = (),
    resource_fields: GateItem | None = None,
) -> RequirementEvaluation:
    status: EvaluationStatus = (
        "missing"
        if not present
        else "invalid"
        if problems
        else "valid"
        if requirement.validator
        else "present"
    )
    error = requirement.error if not present else problems[0] if problems else ""
    item: GateItem = {
        "id": f"resource:{requirement.role}",
        "kind": "resource",
        "role": requirement.role,
        "label": requirement.label,
        "satisfied": present and not problems,
        "status": status,
        "gate": requirement.gate,
    }
    if requirement.validator:
        item["validator"] = requirement.validator
    if resource_fields is not None:
        item.update(resource_fields)
    if not present:
        item["missing"] = requirement.missing or f"{requirement.role} resource"
    if problems:
        item["problems"] = list(problems)
    return RequirementEvaluation(
        role=requirement.role,
        status=status,
        blocker_code=(
            requirement.gate or f"{requirement.role}_missing"
            if not present
            else f"{requirement.role}_invalid" if problems else ""
        ),
        enforcement_error=error,
        problems=problems,
        items=(item,),
    )


__all__ = ["GateEvaluation", "RequirementEvaluation"]
