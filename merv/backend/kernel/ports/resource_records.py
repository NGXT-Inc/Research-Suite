"""Ports used by resource record services."""

from __future__ import annotations

from typing import Protocol, TypedDict


class ResourceObservation(TypedDict):
    """Repo-relative file facts submitted by the data plane."""

    path: str
    kind: str
    title: str
    created_by: str
    mtime_ns: int
    ctime_ns: int
    size_bytes: int
    content_sha256: str
    content_type: str


class ResourceObserver(Protocol):
    """Local data-plane observation required before resource recording."""

    def observe_file(
        self,
        *,
        path: str,
        kind: str = "other",
        title: str = "",
        created_by: str = "codex",
    ) -> ResourceObservation:
        ...


class ResourceAssociationPolicy(Protocol):
    """Resource association validation required by resource recording."""

    def validate_resource_association(self, *, target_type: str, role: str) -> None:
        ...
