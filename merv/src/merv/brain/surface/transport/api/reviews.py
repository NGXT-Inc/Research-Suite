"""Reviews HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from .shared import JsonBody

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api
    @api_router.get("/api/projects/{project_id}/reviews")
    def reviews(project_id: str, target_type: str = "experiment", target_id: str | None = None) -> dict[str, Any]:
        if not target_id:
            return api.review_queue(project_id=project_id)
        return api.call_tool(name="review.status", arguments={"project_id": project_id, "target_type": target_type, "target_id": target_id})

    @api_router.post("/api/projects/{project_id}/reviews/request", status_code=201)
    def request_review(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api.call_tool(name="review.request", arguments={"project_id": project_id, **(body or {})})

    @api_router.post("/api/projects/{project_id}/reviews/start")
    def start_review(
        project_id: str,
        body: JsonBody = Body(default=None),
    ) -> dict[str, Any]:
        return api.start_review(
            project_id=project_id,
            body=body or {},
            tenant_id=None,
        )

    @api_router.post("/api/projects/{project_id}/reviews/submit")
    def submit_review(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api.submit_review(project_id=project_id, body=body or {})


    return api_router
