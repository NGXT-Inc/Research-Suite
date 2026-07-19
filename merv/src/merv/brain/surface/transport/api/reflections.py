"""Reflections HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response, StreamingResponse

from .... import __version__
from ...identity import LOCAL_PRINCIPAL
from ....kernel.utils import NotFoundError, ValidationError
from ....kernel.version import meta
from .shared import JsonBody, conditional_json

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api
    surface = ctx.surface
    api_for_project = ctx.api_for_project
    route_call_tool = ctx.route_call_tool
    @api_router.get("/api/projects/{project_id}/reflections")
    def list_reflections(project_id: str) -> dict[str, Any]:
        # Reflection waves + staleness/coverage signal for the UI panel.
        return api_for_project(project_id).reflections_view(project_id=project_id)

    @api_router.get("/api/projects/{project_id}/reflections/current/graph")
    def project_logic_graph(project_id: str) -> dict[str, Any]:
        # The living project logic graph; same payload shape as the
        # per-experiment graph endpoint. UI-only read, no agent tool.
        return api_for_project(project_id).project_logic_graph(project_id=project_id)

    @api_router.get("/api/projects/{project_id}/reflections/{reflection_id}/graph")
    def reflection_graph(project_id: str, reflection_id: str) -> dict[str, Any]:
        # One wave's logic graph, rendered from the bytes that wave pinned, so
        # a past wave shows faithfully even after later waves overwrite the
        # living file. Same payload shape as /reflections/current/graph (minus
        # signal). Registered after the literal current/graph route so
        # "current" is not captured as a reflection_id. UI-only read.
        return api_for_project(project_id).reflection_graph(
            project_id=project_id, reflection_id=reflection_id
        )

    @api_router.get("/api/projects/{project_id}/reflections/{reflection_id}")
    def get_reflection(project_id: str, reflection_id: str) -> dict[str, Any]:
        return api_for_project(project_id).reflection_detail(
            project_id=project_id, reflection_id=reflection_id
        )


    return api_router
