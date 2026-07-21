"""Best-effort resolution of Research-owned graph references."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any

from ..kernel.state.store import BaseStateStore


@dataclass(frozen=True)
class GraphRefType:
    prefix: str
    entity_type: str
    id_key: str
    query: str
    fields: tuple[str, ...]


GRAPH_REF_TYPES: tuple[GraphRefType, ...] = (
    GraphRefType(
        prefix="rev_",
        entity_type="review",
        id_key="review_id",
        query=(
            "SELECT id, role, verdict, created_at FROM reviews"
            " WHERE id = ? AND project_id = ?"
        ),
        fields=("role", "verdict", "created_at"),
    ),
    GraphRefType(
        prefix="claim_",
        entity_type="claim",
        id_key="claim_id",
        query=(
            "SELECT id, statement, status FROM claims"
            " WHERE id = ? AND project_id = ?"
        ),
        fields=("statement", "status"),
    ),
    GraphRefType(
        prefix="exp_",
        entity_type="experiment",
        id_key="experiment_id",
        query=(
            "SELECT id, intent, status FROM experiments"
            " WHERE id = ? AND project_id = ?"
        ),
        fields=("intent", "status"),
    ),
    GraphRefType(
        prefix="syn_",
        entity_type="reflection",
        id_key="reflection_id",
        query=(
            "SELECT id, title, status, published_at FROM reflections"
            " WHERE id = ? AND project_id = ?"
        ),
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
            result: dict[str, Any] = {}
            for ref in refs:
                ref_type = _type_for_ref(ref)
                if ref_type is None:
                    continue
                row = conn.execute(ref_type.query, (ref, project_id)).fetchone()
                result[ref] = (
                    _record_ref(ref_type=ref_type, row=row)
                    if row
                    else {"type": "unknown", "resolved": False}
                )
            return result


def _type_for_ref(ref: str) -> GraphRefType | None:
    for ref_type in GRAPH_REF_TYPES:
        if ref.startswith(ref_type.prefix):
            return ref_type
    return None


def _record_ref(*, ref_type: GraphRefType, row: Any) -> dict[str, Any]:
    result = {
        "type": ref_type.entity_type,
        "resolved": True,
        ref_type.id_key: row["id"],
    }
    for field in ref_type.fields:
        result[field] = row[field]
    return result
