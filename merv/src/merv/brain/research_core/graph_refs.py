"""Best-effort resolution of Research-owned graph references."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any

from ..kernel.state.store import BaseStateStore

_GRAPH_REF_BATCH_SIZE = 400


@dataclass(frozen=True)
class GraphRefType:
    prefix: str
    entity_type: str
    id_key: str
    table: str
    fields: tuple[str, ...]


GRAPH_REF_TYPES: tuple[GraphRefType, ...] = (
    GraphRefType(
        prefix="rev_",
        entity_type="review",
        id_key="review_id",
        table="reviews",
        fields=("role", "verdict", "created_at"),
    ),
    GraphRefType(
        prefix="claim_",
        entity_type="claim",
        id_key="claim_id",
        table="claims",
        fields=("statement", "status"),
    ),
    GraphRefType(
        prefix="exp_",
        entity_type="experiment",
        id_key="experiment_id",
        table="experiments",
        fields=("intent", "status"),
    ),
    GraphRefType(
        prefix="syn_",
        entity_type="reflection",
        id_key="reflection_id",
        table="reflections",
        fields=("title", "status", "published_at"),
    ),
)


class GraphRefResolver:
    """Resolve only graph references owned by Research."""

    def __init__(self, *, store: BaseStateStore) -> None:
        self.store = store

    def resolve_index(
        self, *, project_id: str, refs: tuple[str, ...]
    ) -> dict[str, Any]:
        if not refs:
            return {}
        with closing(self.store.connect()) as conn:
            resolved: dict[str, Any] = {}
            for ref_type in GRAPH_REF_TYPES:
                typed_refs = tuple(
                    dict.fromkeys(ref for ref in refs if ref.startswith(ref_type.prefix))
                )
                if not typed_refs:
                    continue
                fields = ", ".join(("id", *ref_type.fields))
                by_id: dict[str, Any] = {}
                for start in range(0, len(typed_refs), _GRAPH_REF_BATCH_SIZE):
                    batch = typed_refs[start : start + _GRAPH_REF_BATCH_SIZE]
                    placeholders = ", ".join("?" for _ in batch)
                    rows = conn.execute(
                        f"SELECT {fields} FROM {ref_type.table} "
                        f"WHERE project_id = ? AND id IN ({placeholders})",
                        (project_id, *batch),
                    ).fetchall()
                    by_id.update((str(row["id"]), row) for row in rows)
                for ref in typed_refs:
                    row = by_id.get(ref)
                    resolved[ref] = (
                        _record_ref(ref_type=ref_type, row=row)
                        if row
                        else {"type": "unknown", "resolved": False}
                    )
            return {ref: resolved[ref] for ref in refs if ref in resolved}


def _record_ref(*, ref_type: GraphRefType, row: Any) -> dict[str, Any]:
    result = {
        "type": ref_type.entity_type,
        "resolved": True,
        ref_type.id_key: row["id"],
    }
    for field in ref_type.fields:
        result[field] = row[field]
    return result
