"""Project memory service."""

from __future__ import annotations

import json
from typing import Any

from ..kernel.utils import NotFoundError, ValidationError
from ..kernel.utils import new_id
from ..kernel.state.store import BaseStateStore, row_to_dict
from ..kernel.utils import now_iso


MIN_PROJECT_NAME_LEN = 3


def parse_settings(raw: Any) -> dict[str, Any]:
    """settings_json text -> dict; {} on missing or malformed values."""
    try:
        settings = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return settings if isinstance(settings, dict) else {}


def project_settings(*, conn, project_id: str) -> dict[str, Any]:
    """A project's policy-knob settings (e.g. require_verified_reviews)."""
    row = conn.execute(
        "SELECT settings_json FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return parse_settings(row["settings_json"]) if row else {}


class ProjectService:
    """Owns project metadata."""

    def __init__(self, *, store: BaseStateStore) -> None:
        self.store = store

    def create(
        self,
        *,
        name: str,
        summary: str = "",
        tenant_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        name = self._validate_name(name)
        tenant_id = (tenant_id or "local").strip() or "local"
        with self.store.transaction() as conn:
            project_id = new_id(prefix="proj")
            conn.execute(
                """
                INSERT INTO projects (id, name, summary, tenant_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    name,
                    summary.strip(),
                    tenant_id,
                    now_iso(),
                ),
            )
            if user_id:
                # Authenticated creator becomes the project's first member.
                conn.execute(
                    "INSERT INTO project_members (project_id, user_id, added_at) VALUES (?, ?, ?)",
                    (project_id, user_id, now_iso()),
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
        require_verified_reviews: bool | None = None,
        hidden: bool | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"project not found: {project_id}")
            next_name = row["name"] if name is None else self._validate_name(name)
            next_summary = row["summary"] if summary is None else summary.strip()
            settings = parse_settings(row["settings_json"])
            if require_verified_reviews is not None:
                settings["require_verified_reviews"] = bool(require_verified_reviews)
            if hidden is not None:
                # Stash out of / restore into the UI project list, data retained.
                settings["hidden"] = bool(hidden)
            conn.execute(
                """
                UPDATE projects
                SET name = ?, summary = ?, settings_json = ?
                WHERE id = ?
                """,
                (
                    next_name,
                    next_summary,
                    json.dumps(settings, sort_keys=True),
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
                    "settings": settings,
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

    def list_projects(
        self,
        *,
        tenant_id: str | None = None,
        include_hidden: bool = False,
        user_id: str = "",
    ) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            if user_id:
                # Authenticated (hosted) callers see only projects they are a
                # member of; the local surface passes no user_id and sees all.
                rows = conn.execute(
                    """
                    SELECT p.* FROM projects p
                    JOIN project_members m ON m.project_id = p.id AND m.user_id = ?
                    ORDER BY p.created_at, p.id
                    """,
                    (user_id,),
                ).fetchall()
            elif tenant_id is None:
                rows = conn.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM projects WHERE tenant_id = ? ORDER BY created_at, id",
                    (tenant_id,),
                ).fetchall()
            views = [self._project_view(row=row) for row in rows]
            if not include_hidden:
                views = [v for v in views if not v["settings"].get("hidden")]
            return {"projects": views}
        finally:
            conn.close()

    def current(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        projects = self.list_projects(tenant_id=tenant_id)["projects"]
        if not projects:
            return {
                "exists": False,
                "project": None,
                "hint": (
                    "No Merv project exists yet. Ask the user what project "
                    "name and short summary to use, then call the project tool with "
                    'action="connect".'
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
        view = {
            key: data[key]
            for key in (
                "id",
                "name",
                "summary",
                "status",
                "created_at",
            )
            if key in data
        }
        view["settings"] = parse_settings(data.get("settings_json"))
        return view

    @staticmethod
    def _validate_name(name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise ValidationError("name is required")
        if len(name) < MIN_PROJECT_NAME_LEN:
            raise ValidationError(
                f"name must be at least {MIN_PROJECT_NAME_LEN} characters"
            )
        return name
