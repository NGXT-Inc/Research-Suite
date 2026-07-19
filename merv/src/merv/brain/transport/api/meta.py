"""Meta HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response, StreamingResponse

from ... import __version__
from ...services.identity import LOCAL_PRINCIPAL
from ...kernel.utils import NotFoundError, ValidationError
from ...kernel.version import meta
from .shared import JsonBody, conditional_json

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api
    surface = ctx.surface
    @api_router.get("/health")
    def health() -> dict[str, Any]:
        # Surface hygiene: /health is liveness only and never exposes host
        # paths or local data-plane details.
        return api.health()

    @api_router.get("/api/meta")
    def server_meta() -> dict[str, Any]:
        # Version/compat handshake (cloud plan Phase 9): the server version plus
        # the minimum MCP proxy versions it will serve. Floors are code
        # constants; mode/capabilities tell browser clients which local
        # data-plane actions to hide before requests start getting rejected.
        payload = meta()
        payload["mode"] = "control" if surface.hosted_control else "local"
        payload["capabilities"] = {
            "hosted_control": surface.hosted_control,
            "local_data_plane_http": surface.allow_data_plane_http,
            **surface.data_plane_http_capabilities(),
        }
        # Auth handshake: tells the UI whether to show a login and which
        # Supabase project to sign in against (public values only).
        payload["auth"] = ctx.auth_meta or {"required": False}
        return payload

    @api_router.api_route(
        "/api/daemon/{_path:path}",
        methods=["GET", "POST", "PUT", "DELETE"],
        status_code=410,
    )
    def daemon_retired(_path: str) -> dict[str, Any]:
        # Tombstone for pre-0.0010 local thin-pipe daemons: their long-poll
        # would spin on bare 404s forever with nothing telling the operator why.
        return {
            "error_code": "daemon_retired",
            "message": (
                "The local thin-pipe daemon path was removed in plugin 0.0010; "
                "the stdio MCP proxy now performs local file work itself and "
                "dials MERV_CONTROL_URL. Stop this daemon and "
                "upgrade the merv package."
            ),
        }

    @api_router.get("/api/activity")
    def activity(
        request: Request,
        limit: int = Query(100, ge=1),
        source: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return api.activity(limit=limit, source=source, project_id=project_id)

    # /api/debug/* expose tool-call internals. Hosted control is currently a
    # private operator surface; the real auth system should gate these before
    # broad exposure.
    @api_router.get("/api/debug/tool-calls")
    def tool_call_stats(
        request: Request,
        minutes: int | None = Query(None, ge=1),
        source: str | None = None,
        status: str | None = None,
        tool: str | None = None,
        project_id: str | None = None,
        limit: int = Query(200, ge=1, le=2000),
        sort: str = "ts",
        order: str = "desc",
    ) -> dict[str, Any]:
        return api.tool_call_stats(
            minutes=minutes,
            source=source,
            status=status,
            tool=tool,
            project_id=project_id,
            project_ids=None,
            limit=limit, sort=sort, order=order,
        )

    @api_router.get("/api/debug/tool-calls/{call_id}")
    def tool_call_detail(call_id: int, request: Request) -> dict[str, Any]:
        return api.tool_call_detail(
            call_id=call_id,
            project_ids=None,
        )

    @api_router.post("/api/debug/tool-calls/clear")
    def tool_calls_clear(request: Request) -> dict[str, Any]:
        return api.tool_calls_clear(project_ids=None)


    return api_router
