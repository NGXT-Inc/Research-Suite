"""Proxy-local repository-to-project identity and link orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from .errors import UpstreamError
from .project_links import ProjectLinks, default_project_links_path


ToolCall = Callable[..., dict[str, Any]]


class ProjectScope:
    def __init__(self, *, repo_root: Path, links_path: Optional[Path]) -> None:
        self.repo_root = repo_root
        self.links_path = links_path
        self._links: Optional[ProjectLinks] = None

    @property
    def links(self) -> ProjectLinks:
        if self._links is None:
            self._links = ProjectLinks(
                db_path=self.links_path or default_project_links_path()
            )
        return self._links

    def resolve(self) -> Optional[str]:
        return self.links.project_for_repo(repo_root=str(self.repo_root))

    def require(self) -> str:
        project_id = self.resolve()
        if not project_id:
            raise UpstreamError(
                "no hosted project link found for repo; call the project tool "
                'with action="connect" to link this folder to a project',
                error_code="project_not_linked",
                details={"repo_root": str(self.repo_root)},
            )
        return project_id

    def current(self, *, cloud_call: ToolCall) -> dict[str, Any]:
        project_id = self.resolve()
        if not project_id:
            return self.unlinked()
        project = dict(
            cloud_call(name="project.get", arguments={"project_id": project_id})
        )
        project["repo_root"] = str(self.repo_root)
        return {"exists": True, "project": project, "repo_root": str(self.repo_root)}

    def unlinked(self) -> dict[str, Any]:
        return {
            "exists": False,
            "project": None,
            "repo_root": str(self.repo_root),
            "hint": (
                "No hosted Merv project is linked for this folder. "
                "Ask the user which existing project_id to link and call the "
                'project tool with action="connect" and that project_id; or '
                "ask for a project name and short summary and call it with "
                'action="connect" and name/summary to create and link in one step.'
            ),
        }

    def connect(
        self, *, arguments: dict[str, Any], cloud_call_raw: ToolCall
    ) -> dict[str, Any]:
        project_id = str(arguments.get("project_id") or "").strip()
        name = str(arguments.get("name") or "").strip()
        summary = str(arguments.get("summary") or "").strip()
        overwrite = bool(arguments.get("overwrite") or False)
        if bool(project_id) == bool(name):
            raise UpstreamError(
                "pass exactly one of project_id (link an existing project) or "
                "name (create a new project and link it)",
                error_code="validation_error",
            )
        existing = self.resolve()
        if existing and existing != project_id and not overwrite:
            raise UpstreamError(
                f"this folder is already linked to {existing}; pass overwrite=true to re-link it",
                error_code="already_linked",
                details={"project_id": existing},
            )
        created = False
        if project_id:
            project = dict(
                cloud_call_raw(name="project.get", arguments={"project_id": project_id})
            )
        else:
            project = dict(
                cloud_call_raw(
                    name="project",
                    arguments={"action": "create", "name": name, "summary": summary},
                )
            )
            created = True
            project_id = str(project.get("id") or "")
            if not project_id:
                raise UpstreamError(
                    "project create returned no project id",
                    error_code="daemon_bad_response",
                    details={"project": project},
                )
        self.links.link(repo_root=str(self.repo_root), project_id=project_id)
        return {
            "linked": True,
            "created": created,
            "project": project,
            "repo_root": str(self.repo_root),
        }
