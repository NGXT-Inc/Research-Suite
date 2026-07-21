"""Read-only Storage ledger projection used by Application queries."""

from __future__ import annotations

from contextlib import closing
from typing import Any, cast

from ..application.ports.storage import ProducedObject
from ..kernel.state.store import BaseStateStore, row_to_dict


_PRODUCED_OBJECT_COLUMNS = tuple(ProducedObject.__annotations__)
_EXPERIMENT_ID_BATCH_SIZE = 400


class StorageObjectCatalog:
    """Batch experiment-produced objects without requiring a byte provider."""

    def __init__(self, *, store: BaseStateStore) -> None:
        self.store = store

    def by_experiment(
        self, *, project_id: str, experiment_ids: tuple[str, ...]
    ) -> dict[str, list[ProducedObject]]:
        ids = tuple(dict.fromkeys(str(item) for item in experiment_ids if item))
        result: dict[str, list[ProducedObject]] = {item: [] for item in ids}
        if not ids:
            return result
        columns = ", ".join(_PRODUCED_OBJECT_COLUMNS)
        with closing(self.store.connect()) as conn:
            resolved_project_id = self.store.require_project_id(
                conn=conn, project_id=project_id
            )
            for start in range(0, len(ids), _EXPERIMENT_ID_BATCH_SIZE):
                batch = ids[start : start + _EXPERIMENT_ID_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT {columns}, producing_experiment_id
                    FROM storage_objects
                    WHERE project_id = ?
                      AND producing_experiment_id IN ({placeholders})
                      AND status != 'deleted'
                    ORDER BY producing_experiment_id, kind, name,
                             version DESC, created_seq DESC
                    """,
                    (resolved_project_id, *batch),
                ).fetchall()
                for row in rows:
                    data = row_to_dict(row=row) or {}
                    experiment_id = str(data.pop("producing_experiment_id"))
                    result[experiment_id].append(cast(ProducedObject, data))
        return result


__all__ = ["StorageObjectCatalog"]
