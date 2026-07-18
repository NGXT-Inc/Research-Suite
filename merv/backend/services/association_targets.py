"""Research-core resolution of resource-association targets.

Injected into the artifacts module at composition so artifacts never names
research-core tables (import law allows research_core -> artifacts only).
"""

from __future__ import annotations

from ..state.store import Connection
from ..utils import NotFoundError, ValidationError

_TABLE_BY_TYPE = {
    "experiment": "experiments",
    "reflection": "reflections",
    "claim": "claims",
    "review": "reviews",
}
# Experiments and reflections scope associations to their current attempt, so
# a review rejection that bumps the attempt naturally invalidates stale
# associations for either target kind.
_ATTEMPT_TABLE_BY_TYPE = {"experiment": "experiments", "reflection": "reflections"}


class AssociationTargets:
    """Existence and attempt scoping for association targets (RC-owned SQL)."""

    def project_id_for(
        self, *, conn: Connection, target_type: str, target_id: str
    ) -> str | None:
        if target_type == "attempt":
            # Attempts are implicit in v0.0001.
            return None
        table = _TABLE_BY_TYPE.get(target_type)
        if table is None:
            raise ValidationError(f"unsupported target type: {target_type}")
        row = conn.execute(
            f"SELECT id, project_id FROM {table} WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{target_type} not found: {target_id}")
        return str(row["project_id"])

    def attempt_index_for(
        self, *, conn: Connection, target_type: str, target_id: str
    ) -> int:
        table = _ATTEMPT_TABLE_BY_TYPE.get(target_type)
        if table is None:
            return 0
        row = conn.execute(
            f"SELECT attempt_index FROM {table} WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{target_type} not found: {target_id}")
        return int(row["attempt_index"])
