"""Projects HTTP routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response, StreamingResponse

from ... import __version__
from ...services.identity import LOCAL_PRINCIPAL
from ...utils import NotFoundError, ValidationError
from ...version import meta
from .shared import JsonBody, conditional_json_from_signal

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api
    api_for_project = ctx.api_for_project
    route_call_tool = ctx.route_call_tool

    def _request_user_id(request: Request) -> str:
        principal = getattr(request.state, "principal", LOCAL_PRINCIPAL)
        return str(getattr(principal, "user_id", "") or "")

    @api_router.get("/api/projects")
    def list_projects(request: Request) -> dict[str, Any]:
        user_id = _request_user_id(request)
        return api.call_tool(
            name="project.list",
            arguments={},
            internal_kwargs={"user_id": user_id} if user_id else None,
        )

    @api_router.post("/api/projects", status_code=201)
    def create_project(request: Request, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        payload = body or {}
        principal = getattr(request.state, "principal", LOCAL_PRINCIPAL)
        return api.create_project(
            body=payload,
            tenant_id=str(getattr(principal, "tenant_id", "") or "") or None,
            user_id=_request_user_id(request),
        )

    @api_router.get("/api/projects/{project_id}/members")
    def list_members(project_id: str) -> dict[str, Any]:
        return {"members": api.app.store.list_project_members(project_id=project_id)}

    @api_router.post("/api/projects/{project_id}/members", status_code=201)
    def add_member(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        # Any member may share the project (the membership gate already ran).
        user_id = str((body or {}).get("user_id") or "").strip()
        if not user_id:
            raise ValidationError("user_id is required", details={"field": "user_id"})
        api.app.projects.get(project_id=project_id)
        api.app.store.add_project_member(project_id=project_id, user_id=user_id)
        return {"members": api.app.store.list_project_members(project_id=project_id)}

    @api_router.delete("/api/projects/{project_id}/members/{user_id}")
    def remove_member(project_id: str, user_id: str) -> dict[str, Any]:
        api.app.store.remove_project_member(project_id=project_id, user_id=user_id)
        return {"members": api.app.store.list_project_members(project_id=project_id)}

    @api_router.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="project.get", arguments={"project_id": project_id})

    @api_router.patch("/api/projects/{project_id}")
    @api_router.put("/api/projects/{project_id}")
    def update_project(
        project_id: str, request: Request, body: JsonBody = Body(default=None)
    ) -> dict[str, Any]:
        return route_call_tool(
            name="project.update",
            arguments={**(body or {}), "project_id": project_id},
            activity_source="http",
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )

    @api_router.get("/api/projects/{project_id}/home")
    def home(project_id: str, request: Request) -> Response:
        # Composite signal ETag. The home payload is a pure function of three
        # inputs: the event ledger (claims/experiments/reviews/reflections/
        # resources all append events), live sandbox rows (heartbeats bump
        # updated_at but write no event), and the MLflow reachability probe
        # (external, 5s-cached). A 304 skips the heavy status/experiment render.
        target = api_for_project(project_id)
        store = target.app.store
        return conditional_json_from_signal(
            request,
            signal_parts=(
                "home",
                project_id,
                store.project_event_signal(project_id=project_id),
                store.project_sandbox_signal(project_id=project_id),
                json.dumps(
                    target.app.mlflow_tracking.health(),
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
            ),
            payload=lambda: target.home(project_id=project_id),
        )

    @api_router.get("/api/projects/{project_id}/status")
    def project_status(project_id: str, experiment_id: str | None = None) -> dict[str, Any]:
        # Full shape for the UI (see home()); the tool stays slim for the agent.
        target = api_for_project(project_id)
        return target._present(
            target.app.workflow.status_and_next(
                project_id=project_id, experiment_id=experiment_id
            )
        )

    return api_router
