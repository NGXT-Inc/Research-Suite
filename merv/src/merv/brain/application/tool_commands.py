"""Application-owned decisions behind merged control-plane tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..kernel.utils import ValidationError

Command = Callable[..., dict[str, Any]]


class AgentExperimentList(Protocol):
    def __call__(
        self, *, project_id: str | None = None
    ) -> dict[str, Any]: ...


@dataclass(kw_only=True, slots=True)
class ControlToolOperations:
    project_create: Command
    project_get: Command
    claims_list: Command
    list_agent_experiments: AgentExperimentList
    resource_resolve: Command
    resources_list: Command
    storage_resolve: Command | None
    storage_list: Command | None
    storage_actions: dict[str, Command]

    def experiment_list(self, *, project_id: str | None = None) -> dict[str, Any]:
        return self.list_agent_experiments(project_id=project_id)

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
    ) -> dict[str, Any]:
        if action == "create":
            return self.project_create(
                name=name, summary=summary, tenant_id=tenant_id, user_id=user_id
            )
        if action == "overview":
            project = self.project_get(project_id=project_id)
            return {
                "project": {
                    "id": project["id"],
                    "name": project["name"],
                    "summary": project.get("summary", ""),
                },
                "claims": self.claims_list(project_id=project_id)["claims"],
                "experiments": self.experiment_list(project_id=project_id)["experiments"],
            }
        raise ValidationError(
            f'project action="{action}" is served by the local merv '
            "proxy, not the brain. Seeing this means your Merv client "
            "is older than the brain — update the plugin (git pull) and restart "
            "your MCP client."
        )

    def resource_find(
        self,
        *,
        resource_id: str | None = None,
        include_history: bool = False,
        kind: str | None = None,
        experiment_id: str | None = None,
        missing: bool | None = None,
        compact: bool = False,
        limit: int | None = None,
        offset: int = 0,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if resource_id is not None:
            return self.resource_resolve(
                resource_id=resource_id,
                include_history=include_history,
                project_id=project_id,
            )
        return self.resources_list(
            kind=kind,
            experiment_id=experiment_id,
            missing=missing,
            compact=compact,
            limit=limit,
            offset=offset,
            project_id=project_id,
        )

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
        assert self.storage_resolve is not None and self.storage_list is not None
        if object_id or name:
            return self.storage_resolve(
                project_id=project_id,
                object_id=object_id,
                name=name,
                version=version,
                include_download=include_download,
            )
        return self.storage_list(
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
        operation = self.storage_actions.get(action)
        if operation is None:
            raise ValidationError(f"unknown storage object action: {action}")
        return operation(project_id=project_id, object_id=object_id)
