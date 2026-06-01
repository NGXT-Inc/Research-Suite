"""Project memory service."""

from __future__ import annotations

from typing import Any

from ..utils import NotFoundError, ValidationError
from ..utils import new_id
from ..state.store import StateStore, row_to_dict
from ..utils import now_iso


class ProjectService:
    """Owns project metadata."""

    def __init__(self, *, store: StateStore) -> None:
        self.store = store

    def create(self, *, name: str, summary: str = "") -> dict[str, Any]:
        if not name.strip():
            raise ValidationError("name is required")
        with self.store.transaction() as conn:
            project_id = new_id(prefix="proj")
            conn.execute(
                "INSERT INTO projects (id, name, summary, created_at) VALUES (?, ?, ?, ?)",
                (project_id, name.strip(), summary.strip(), now_iso()),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="project.created",
                target_type="project",
                target_id=project_id,
                payload={"name": name},
            )
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return row_to_dict(row=row) or {}

    def update(
        self,
        *,
        project_id: str | None = None,
        name: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"project not found: {project_id}")
            next_name = row["name"] if name is None else name.strip()
            next_summary = row["summary"] if summary is None else summary.strip()
            conn.execute(
                "UPDATE projects SET name = ?, summary = ? WHERE id = ?",
                (next_name, next_summary, project_id),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="project.updated",
                target_type="project",
                target_id=project_id,
                payload={"name": next_name, "summary": next_summary},
            )
            updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return row_to_dict(row=updated) or {}

    def get(self, *, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"project not found: {project_id}")
            return row_to_dict(row=row) or {}
        finally:
            conn.close()

    def list_projects(self) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
            return {"projects": [row_to_dict(row=row) or {} for row in rows]}
        finally:
            conn.close()
