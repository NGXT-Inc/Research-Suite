"""Reflections HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api
    @api_router.get("/api/projects/{project_id}/reflections")
    def list_reflections(project_id: str) -> dict[str, Any]:
        # Reflection waves + staleness/coverage signal for the UI panel.
        return api.reflections_view(project_id=project_id)

    @api_router.get("/api/projects/{project_id}/reflections/current/graph")
    def project_logic_graph(project_id: str) -> dict[str, Any]:
        # The living project logic graph; same payload shape as the
        # per-experiment graph endpoint. UI-only read, no agent tool.
        return api.project_logic_graph(project_id=project_id)

    @api_router.get("/api/projects/{project_id}/reflections/{reflection_id}/graph")
    def reflection_graph(project_id: str, reflection_id: str) -> dict[str, Any]:
        # One wave's logic graph, rendered from the bytes that wave pinned, so
        # a past wave shows faithfully even after later waves overwrite the
        # living file. Same payload shape as /reflections/current/graph (minus
        # signal). Registered after the literal current/graph route so
        # "current" is not captured as a reflection_id. UI-only read.
        return api.reflection_graph(
            project_id=project_id, reflection_id=reflection_id
        )

    @api_router.get("/api/projects/{project_id}/reflections/{reflection_id}")
    def get_reflection(project_id: str, reflection_id: str) -> dict[str, Any]:
        return api.reflection_detail(
            project_id=project_id, reflection_id=reflection_id
        )


    return api_router
