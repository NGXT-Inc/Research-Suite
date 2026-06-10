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

    def create(
        self,
        *,
        name: str,
        summary: str = "",
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValidationError("name is required")
        with self.store.transaction() as conn:
            project_id = new_id(prefix="proj")
            conn.execute(
                """
                INSERT INTO projects (id, name, summary, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    project_id,
                    name.strip(),
                    summary.strip(),
                    now_iso(),
                ),
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
            return self._project_view(row=row)

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
                """
                UPDATE projects
                SET name = ?, summary = ?
                WHERE id = ?
                """,
                (
                    next_name,
                    next_summary,
                    project_id,
                ),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="project.updated",
                target_type="project",
                target_id=project_id,
                payload={
                    "name": next_name,
                    "summary": next_summary,
                },
            )
            updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return self._project_view(row=updated)

    def get(self, *, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"project not found: {project_id}")
            return self._project_view(row=row)
        finally:
            conn.close()

    def list_projects(self) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
            return {"projects": [self._project_view(row=row) for row in rows]}
        finally:
            conn.close()

    def current(self) -> dict[str, Any]:
        projects = self.list_projects()["projects"]
        if not projects:
            return {
                "exists": False,
                "project": None,
                "hint": (
                    "No Research Plugin project exists yet. Ask the user what project "
                    "name and short summary to use, then call project.create."
                ),
            }
        if len(projects) > 1:
            raise ValidationError(
                "multiple projects exist in this state store; use project.get with an explicit project_id",
                details={"project_ids": [project["id"] for project in projects]},
            )
        return {"exists": True, "project": projects[0]}

    def _project_view(self, *, row) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        return {
            key: data[key]
            for key in ("id", "name", "summary", "created_at")
            if key in data
        }
