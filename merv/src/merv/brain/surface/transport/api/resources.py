"""Resources HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api

    @api_router.get("/api/projects/{project_id}/resources")
    def list_resources(project_id: str, kind: str | None = None) -> dict[str, Any]:
        return api.filter_resources(project_id=project_id, kind=kind)

    @api_router.get("/api/projects/{project_id}/resources/tree")
    def resources_tree(project_id: str) -> dict[str, Any]:
        return api.resources_tree(project_id=project_id)

    @api_router.get("/api/projects/{project_id}/resources/{resource_id}")
    def resolve_resource(
        project_id: str, resource_id: str, request: Request
    ) -> dict[str, Any]:
        return ctx.call_tool(
            request,
            name="resource.find",
            arguments={"project_id": project_id, "resource_id": resource_id},
        )

    @api_router.get("/api/projects/{project_id}/resources/{resource_id}/history")
    def resource_history(project_id: str, resource_id: str) -> dict[str, Any]:
        # UI-only read; the agent tool surface folds history into
        # resource.find(resource_id, include_history=true), so call the service directly.
        return api.app.resources.history(resource_id=resource_id, project_id=project_id)

    @api_router.delete("/api/projects/{project_id}/resources/{resource_id}")
    def delete_resource(
        project_id: str, resource_id: str, request: Request
    ) -> dict[str, Any]:
        return ctx.call_tool(
            request,
            name="resource.delete",
            arguments={"project_id": project_id, "resource_id": resource_id},
        )

    @api_router.get("/api/projects/{project_id}/resources/{resource_id}/content")
    def resource_content(
        project_id: str, resource_id: str, version: str | None = None
    ) -> dict[str, Any]:
        # `version` pins the exact submitted bytes of one resource version
        # (faithful historical rendering for reflection-wave
        # artifacts).
        # Omitted → unchanged behavior (latest gated bytes / live file).
        return api.resource_content(
            project_id=project_id, resource_id=resource_id, version=version
        )

    @api_router.get("/api/projects/{project_id}/resources/{resource_id}/file")
    def resource_file(
        project_id: str, resource_id: str, rel: str | None = None
    ) -> Response:
        content, headers = api.resource_file(
            project_id=project_id, resource_id=resource_id, rel=rel
        )
        content_type = headers.pop("Content-Type", "application/octet-stream")
        return Response(content=content, media_type=content_type, headers=headers)

    return api_router
