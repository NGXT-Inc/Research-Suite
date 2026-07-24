"""Application-owned decisions behind merged control-plane tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..kernel.utils import ValidationError
from ..research_core.facade import ResearchClaims, ResearchProjects
from .experiments.queries import ExperimentCollectionQuery
from .ports.storage import ObjectStorage


@dataclass(kw_only=True, slots=True)
class ControlToolOperations:
    projects: ResearchProjects
    claims: ResearchClaims
    experiments: ExperimentCollectionQuery
    storage: ObjectStorage | None

    def experiment_list(self, *, project_id: str | None = None) -> dict[str, Any]:
        return self.experiments.agent(project_id=project_id)

    def project(
        self,
        *,
        action: str,
        project_id: str = "",
        name: str = "",
        summary: str = "",
        overwrite: bool = False,
        tenant_id: str | None = None,
        user_id: str = "",
        key_project_id: str = "",
    ) -> dict[str, Any]:
        # current/connect reach the brain ONLY for a keyed (cloud) caller: a
        # local proxy resolves them from its folder link before the wire. The
        # key carries project identity, so there is no folder to link.
        if action == "current":
            if not key_project_id:
                return {
                    "exists": False,
                    "hint": "This MCP key is not bound to a project. Mint a "
                    "project key from the RapidReview web app (Settings → MCP "
                    "keys) and reconnect with it.",
                }
            project = self.projects.get(project_id=key_project_id)
            return {
                "exists": True,
                "project": {
                    "id": project["id"],
                    "name": project["name"],
                    "summary": project.get("summary", ""),
                },
            }
        if action == "connect":
            raise ValidationError(
                'project action="connect" links a local folder to a project, '
                "which does not apply to a keyed agent: your MCP key already "
                'carries its project identity. Use action="current" to see it.'
            )
        if action == "create":
            return self.projects.create(
                name=name, summary=summary, tenant_id=tenant_id, user_id=user_id
            )
        if action == "overview":
            # A key defaults to its bound project; a caller may still name one.
            resolved = project_id or key_project_id
            project = self.projects.get(project_id=resolved)
            return {
                "project": {
                    "id": project["id"],
                    "name": project["name"],
                    "summary": project.get("summary", ""),
                },
                "claims": self.claims.list_claims(project_id=resolved)["claims"],
                "experiments": self.experiment_list(project_id=resolved)["experiments"],
            }
        raise ValidationError(f'project action="{action}" is not recognized')

    def storage_find(
        self,
        *,
        project_id: str | None = None,
        object_id: str | None = None,
        name: str | None = None,
        version: int | None = None,
        include_download: bool = True,
        kind: str | None = None,
        status: str | None = None,
        include_expired: bool = False,
        limit: int | None = None,
        offset: int = 0,
        compact: bool = False,
    ) -> dict[str, Any]:
        assert self.storage is not None
        if object_id or name:
            return self.storage.resolve(
                project_id=project_id,
                object_id=object_id,
                name=name,
                version=version,
                include_download=include_download,
            )
        return self.storage.list_objects(
            project_id=project_id,
            kind=kind,
            status=status,
            include_expired=include_expired,
            limit=limit,
            offset=offset,
            compact=compact,
        )

    def storage_object(
        self, *, object_id: str, action: str, project_id: str | None = None
    ) -> dict[str, Any]:
        if self.storage is None or action not in {"pin", "unpin", "renew", "delete"}:
            raise ValidationError(f"unknown storage object action: {action}")
        operation = {
            "pin": self.storage.pin, "unpin": self.storage.unpin,
            "renew": self.storage.renew, "delete": self.storage.delete,
        }[action]
        return operation(project_id=project_id, object_id=object_id)
