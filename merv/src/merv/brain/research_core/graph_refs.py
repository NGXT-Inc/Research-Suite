"""Best-effort project graph reference resolution."""

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
        prefix="res_",
        entity_type="resource",
        id_key="resource_id",
        query=(
            "SELECT id, path, kind, title, missing FROM resources"
            " WHERE id = ? AND project_id = ? AND deleted = 0"
        ),
        fields=("path", "kind", "title", "missing"),
    ),
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
RESOURCE_REF_TYPE = GRAPH_REF_TYPES[0]


class GraphRefResolver:
    """Resolves graph node refs against control-plane records."""

    def __init__(self, *, store: BaseStateStore) -> None:
        self.store = store

    def resolve_index(
        self, *, project_id: str, graph: dict[str, Any] | None
    ) -> dict[str, Any]:
        refs = self._refs_from_graph(graph=graph)
        if not refs:
            return {}
        with closing(self.store.connect()) as conn:
            return {
                ref: self._resolve_one(conn=conn, project_id=project_id, ref=ref)
                for ref in refs
            }

    @staticmethod
    def _refs_from_graph(*, graph: dict[str, Any] | None) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()
        for node in (graph or {}).get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_refs = node.get("refs")
            if not isinstance(node_refs, list):
                continue
            for ref in node_refs:
                if isinstance(ref, str) and ref.strip() and ref not in seen:
                    seen.add(ref)
                    refs.append(ref)
        return refs

    def _resolve_one(self, *, conn, project_id: str, ref: str) -> dict[str, Any]:
        ref_type = _type_for_ref(ref)
        if ref_type is None:
            return self._resolve_path_ref(conn=conn, project_id=project_id, ref=ref)

        row = conn.execute(ref_type.query, (ref, project_id)).fetchone()
        if row:
            return _record_ref(ref_type=ref_type, row=row)
        return {"type": "unknown", "resolved": False}

    @staticmethod
    def _resolve_path_ref(
        *, conn, project_id: str, ref: str
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT id, path, kind, title, missing FROM resources"
            " WHERE project_id = ? AND path = ? AND deleted = 0",
            (project_id, ref),
        ).fetchone()
        if row:
            return _record_ref(ref_type=RESOURCE_REF_TYPE, row=row)
        # Path refs resolve against registered resources only; the control
        # plane cannot probe local working-tree files.
        return {
            "type": "unknown",
            "resolved": False,
            "hint": "not a registered resource path; register the file to make this ref resolvable",
        }


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
        value = row[field]
        result[field] = bool(value) if field == "missing" else value
    return result
