"""Directory-backed project routing for a shared HTTP daemon."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .app import ResearchPluginApp
from .contracts import PROJECT_SCOPED_TOOL_NAMES
from .daemon_marker import clear_marker, write_marker
from .execution import SandboxBackend
from .utils import NotFoundError, ValidationError, now_iso


BackendFactory = Callable[[Path], SandboxBackend | None]


@dataclass(frozen=True)
class ProjectRoute:
    project_id: str
    repo_root: Path


class ProjectRouter:
    """Routes shared-daemon requests to isolated per-directory app instances."""

    def __init__(
        self,
        *,
        registry_db_path: Path,
        execution_backend_factory: BackendFactory | None = None,
        marker_host: str | None = None,
        marker_port: int | None = None,
    ) -> None:
        self.registry_db_path = registry_db_path.expanduser().resolve()
        self.registry_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.execution_backend_factory = execution_backend_factory
        self.marker_host = marker_host
        self.marker_port = marker_port
        self._lock = threading.RLock()
        self._apps_by_repo: dict[Path, ResearchPluginApp] = {}
        self._routes_by_project: dict[str, ProjectRoute] = {}
        self._initialize()
        self._resume_active_sandbox_projects()

    def set_marker_endpoint(self, *, host: str, port: int) -> None:
        self.marker_host = host
        self.marker_port = port
        for route in self._routes_by_project.values():
            self._write_marker(repo_root=route.repo_root)

    def clear_markers(self) -> None:
        for route in self._routes_by_project.values():
            try:
                clear_marker(repo_root=route.repo_root)
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self) -> None:
        for app in list(self._apps_by_repo.values()):
            app.shutdown()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "multi_project",
            "registry": str(self.registry_db_path),
            "projects": len(self._routes_by_project),
        }

    def list_projects(self) -> dict[str, Any]:
        projects: list[dict[str, Any]] = []
        with self._lock:
            routes = list(self._routes_by_project.values())
        for route in routes:
            app = self.app_for_project(route.project_id)
            try:
                project = app.projects.get(project_id=route.project_id)
            except Exception:
                project = {
                    "id": route.project_id,
                    "name": route.repo_root.name,
                    "summary": "",
                    "created_at": "",
                }
            project = dict(project)
            project["repo_root"] = str(route.repo_root)
            projects.append(project)
        projects.sort(key=lambda item: (item.get("created_at") or "", item.get("name") or ""))
        return {"projects": projects}

    def create_project(
        self,
        *,
        repo_root: str | Path,
        name: str,
        summary: str = "",
    ) -> dict[str, Any]:
        repo = self._canonical_repo(repo_root)
        if not name.strip():
            raise ValidationError("name is required")
        with self._lock:
            existing = self._route_for_repo(repo)
            if existing is not None:
                raise ValidationError(
                    "a project already exists for this directory",
                    details={"repo_root": str(repo), "project_id": existing.project_id},
                )
            existing_projects = self._stored_projects(repo)
            if len(existing_projects) > 1:
                raise ValidationError(
                    "this directory has multiple stored projects; shared mode requires one project per directory",
                    details={"repo_root": str(repo), "project_ids": [p["id"] for p in existing_projects]},
                )
            if (
                len(existing_projects) == 1
                and existing_projects[0].get("name") != "Local Research Project"
            ):
                route = self._register_route(project_id=existing_projects[0]["id"], repo_root=repo)
                raise ValidationError(
                    "a project already exists for this directory",
                    details={"repo_root": str(repo), "project_id": route.project_id},
                )
            app = self._app_for_repo_locked(repo)
            projects = app.projects.list_projects()["projects"]
            if len(projects) == 1 and projects[0].get("name") == "Local Research Project":
                project = app.projects.update(
                    project_id=projects[0]["id"],
                    name=name,
                    summary=summary,
                )
            else:
                project = app.call_tool(
                    "project.create",
                    {"name": name, "summary": summary},
                    activity_source="http",
                )
            self._register_route(project_id=project["id"], repo_root=repo)
            project = dict(project)
            project["repo_root"] = str(repo)
            return project

    def project_for_repo(self, *, repo_root: str | Path) -> dict[str, Any] | None:
        repo = self._canonical_repo(repo_root)
        with self._lock:
            route = self._route_for_repo(repo)
            if route is None:
                projects = self._stored_projects(repo)
                if not projects:
                    return None
                if len(projects) > 1:
                    raise ValidationError(
                        "this directory has multiple stored projects; shared mode requires one project per directory",
                        details={"repo_root": str(repo), "project_ids": [p["id"] for p in projects]},
                    )
                route = self._register_route(project_id=projects[0]["id"], repo_root=repo)
            project = self._app_for_repo_locked(repo).projects.get(project_id=route.project_id)
            data = dict(project)
            data["repo_root"] = str(repo)
            return data

    def current_project(self, *, repo_root: str | Path) -> dict[str, Any]:
        repo = self._canonical_repo(repo_root)
        project = self.project_for_repo(repo_root=repo)
        if project is None:
            return {
                "exists": False,
                "project": None,
                "repo_root": str(repo),
                "hint": (
                    "No Research Plugin project is registered for this folder. "
                    "Ask the user what project name and short summary to use, "
                    "then call project.create before using project-scoped tools."
                ),
            }
        return {"exists": True, "project": project, "repo_root": str(repo)}

    def app_for_project(self, project_id: str) -> ResearchPluginApp:
        with self._lock:
            route = self._routes_by_project.get(project_id)
            if route is None:
                route = self._load_route(project_id=project_id)
            if route is None:
                raise NotFoundError(f"project not found: {project_id}")
            return self._app_for_repo_locked(route.repo_root)

    def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        activity_source: str = "http",
    ) -> dict[str, Any]:
        arguments = dict(arguments or {})
        context = context or {}
        repo_root = context.get("repo_root")
        if name == "project.current" and repo_root:
            return self.current_project(repo_root=repo_root)
        if name == "project.list" and repo_root:
            current = self.current_project(repo_root=repo_root)
            if not current["exists"]:
                return {"projects": [], "hint": current["hint"]}
            return {"projects": [current["project"]]}
        if name == "project.create" and context.get("repo_root"):
            project = self.create_project(
                repo_root=context["repo_root"],
                name=str(arguments.get("name") or ""),
                summary=str(arguments.get("summary") or ""),
            )
            return project
        if name == "sandbox.health":
            return self.health()
        project_id = arguments.get("project_id")
        if isinstance(project_id, str) and project_id:
            app = self.app_for_project(project_id)
        elif repo_root:
            project = self.project_for_repo(repo_root=repo_root)
            if project is None:
                raise ValidationError(
                    "no project is registered for this folder; call project.current and ask the user what project to create before calling project.create",
                    details={"repo_root": str(self._canonical_repo(repo_root))},
                )
            app = self.app_for_project(str(project["id"]))
            if name in PROJECT_SCOPED_TOOL_NAMES:
                arguments["project_id"] = project["id"]
        else:
            raise ValidationError("project_id or repo_root context is required")
        return app.call_tool(name=name, arguments=arguments, activity_source=activity_source)

    def list_tools(self) -> list[dict[str, Any]]:
        return self.tool_template_app().list_tools()

    def activity_recent(self, *, limit: int, source: str | None = None) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        for route in list(self._routes_by_project.values()):
            app = self.app_for_project(route.project_id)
            result = app.activity.recent(limit=limit, source=source)
            for event in result["events"]:
                item = dict(event)
                item.setdefault("project_id", route.project_id)
                item.setdefault("repo_root", str(route.repo_root))
                events.append(item)
            summaries.append(result["summary"])
        events = events[-limit:]
        return {"events": events, "summary": {"workspaces": summaries, "count": len(events)}}

    def tool_template_app(self) -> ResearchPluginApp:
        with self._lock:
            if self._apps_by_repo:
                return next(iter(self._apps_by_repo.values()))
            template_repo = self.registry_db_path.parent / "_tool_schema"
            return self._app_for_repo_locked(template_repo)

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS directory_projects (
                  project_id TEXT PRIMARY KEY,
                  repo_root TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL
                )
                """
            )
            rows = conn.execute("SELECT project_id, repo_root FROM directory_projects").fetchall()
            for project_id, repo_root in rows:
                route = ProjectRoute(project_id=str(project_id), repo_root=Path(str(repo_root)))
                self._routes_by_project[route.project_id] = route
            conn.commit()
        finally:
            conn.close()

    def _resume_active_sandbox_projects(self) -> None:
        """Eagerly instantiate apps for registered projects with live sandboxes.

        Apps are otherwise created lazily on the first tool call, but the
        expiration reaper runs inside each app's SandboxService — and Lambda
        VMs have no server-side lifetime enforcement. After a daemon restart, a
        project with a running sandbox would have no reaper (and an expired VM
        would bill forever) until something happened to touch the project.
        Best-effort per project: one unreadable state DB or a backend that
        fails to construct must not block daemon startup or the other
        projects' reapers.
        """
        with self._lock:
            for route in list(self._routes_by_project.values()):
                try:
                    if self._has_active_sandboxes(route.repo_root):
                        self._app_for_repo_locked(route.repo_root)
                except Exception:  # noqa: BLE001
                    pass

    def _has_active_sandboxes(self, repo_root: Path) -> bool:
        db_path = repo_root / ".research_plugin" / "state.sqlite"
        if not db_path.exists():
            return False
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM sandboxes WHERE status IN ('running', 'provisioning') LIMIT 1"
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.registry_db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _canonical_repo(self, repo_root: str | Path) -> Path:
        repo = Path(repo_root).expanduser().resolve()
        repo.mkdir(parents=True, exist_ok=True)
        if not repo.is_dir():
            raise ValidationError(f"project directory is not a directory: {repo}")
        return repo

    def _app_for_repo_locked(self, repo_root: Path) -> ResearchPluginApp:
        app = self._apps_by_repo.get(repo_root)
        if app is not None:
            return app
        backend = self.execution_backend_factory(repo_root) if self.execution_backend_factory else None
        app = ResearchPluginApp(
            repo_root=repo_root,
            db_path=repo_root / ".research_plugin" / "state.sqlite",
            execution_backend=backend,
        )
        self._apps_by_repo[repo_root] = app
        return app

    def _stored_projects(self, repo_root: Path) -> list[dict[str, Any]]:
        db_path = repo_root / ".research_plugin" / "state.sqlite"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, name FROM projects ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def _route_for_repo(self, repo_root: Path) -> ProjectRoute | None:
        for route in self._routes_by_project.values():
            if route.repo_root == repo_root:
                return route
        return None

    def _load_route(self, *, project_id: str) -> ProjectRoute | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT project_id, repo_root FROM directory_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                return None
            route = ProjectRoute(project_id=str(row[0]), repo_root=Path(str(row[1])))
            self._routes_by_project[route.project_id] = route
            return route
        finally:
            conn.close()

    def _register_route(self, *, project_id: str, repo_root: Path) -> ProjectRoute:
        route = ProjectRoute(project_id=project_id, repo_root=repo_root)
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO directory_projects (project_id, repo_root, created_at)
                VALUES (?, ?, ?)
                """,
                (project_id, str(repo_root), now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        self._routes_by_project[project_id] = route
        self._write_marker(repo_root=repo_root)
        return route

    def _write_marker(self, *, repo_root: Path) -> None:
        if not self.marker_host or not self.marker_port:
            return
        try:
            write_marker(repo_root=repo_root, host=self.marker_host, port=self.marker_port)
        except Exception:  # noqa: BLE001
            pass
