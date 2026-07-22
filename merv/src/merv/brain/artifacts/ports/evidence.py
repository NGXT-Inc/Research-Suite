"""Public evidence facts shared with Research.

The contract deliberately exposes immutable submitted evidence, not database
connections, blob locators, or Artifact persistence tables.  Its concrete
implementation remains in Artifacts; Research only decides when the evidence
is required and what workflow policy applies to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AssociatedEvidence:
    """One resource association expressed without persistence-shaped names."""

    resource_id: str
    project_id: str
    path: str
    kind: str
    title: str
    current_version_id: str | None
    version_token: str
    modified_time_ns: int
    size_bytes: int
    observed_at: str
    git_commit: str | None
    is_missing: bool
    is_deleted: bool
    created_by: str
    created_at: str
    updated_at: str
    role: str
    attempt_index: int
    submitted_version_id: str | None
    association_order: int


@dataclass(frozen=True, slots=True)
class SubmittedDocument:
    text: str
    version_id: str
    path: str
    role: str
    figure_links: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubmittedEvidence:
    """Best-effort submitted text for one immutable association."""

    role: str
    path: str
    version_id: str | None
    association_order: int
    content: str | None


@dataclass(frozen=True, slots=True)
class AssociationTarget:
    project_id: str | None
    attempt_index: int


@runtime_checkable
class EvidenceReader(Protocol):
    """Immutable Artifact evidence consumed by Research policy."""

    def resources_for_target(
        self, *, target_type: str, target_id: str
    ) -> tuple[AssociatedEvidence, ...]: ...

    def resources_for_targets(
        self, *, target_type: str, target_ids: tuple[str, ...]
    ) -> dict[str, tuple[AssociatedEvidence, ...]]: ...

    def submitted_document(
        self,
        *,
        version_id: str | None,
        path: str,
        role: str,
        what: str,
    ) -> SubmittedDocument: ...

    def submitted_evidence(
        self,
        *,
        target_type: str,
        target_id: str,
        attempt_index: int,
        roles: tuple[str, ...],
    ) -> tuple[SubmittedEvidence, ...]: ...


@runtime_checkable
class AssociationTargetResolver(Protocol):
    """Research-owned target facts needed while associating a resource."""

    def resolve(self, *, target_type: str, target_id: str) -> AssociationTarget: ...


__all__ = [
    "AssociatedEvidence",
    "AssociationTarget",
    "AssociationTargetResolver",
    "EvidenceReader",
    "SubmittedDocument",
    "SubmittedEvidence",
]
