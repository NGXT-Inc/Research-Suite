"""Application-owned read contract for heavy objects produced by experiments."""

from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


class ProducedObject(TypedDict):
    """Hosted-safe ledger fields used in experiment response composition."""

    id: str
    name: str
    version: int
    kind: str
    content_sha256: str
    size_bytes: int
    content_type: str
    status: str
    expires_at: str | None
    producing_run: str
    source_uri: str
    notes: str
    created_at: str
    updated_at: str
    last_accessed_at: str | None


@runtime_checkable
class ProducedObjectCatalog(Protocol):
    """Batch heavy-object facts for experiment response presentation."""

    def by_experiment(
        self, *, project_id: str, experiment_ids: tuple[str, ...]
    ) -> dict[str, list[ProducedObject]]: ...


__all__ = ["ProducedObject", "ProducedObjectCatalog"]
