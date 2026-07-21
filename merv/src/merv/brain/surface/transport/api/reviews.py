"""Reviews HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request

from .shared import JsonBody, path_scoped_body

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api

    @api_router.get("/api/projects/{project_id}/reviews")
    def reviews(
        project_id: str,
        request: Request,
        target_type: str = "experiment",
        target_id: str | None = None,
    ) -> dict[str, Any]:
        if not target_id:
            return api.app.reviews.queue(project_id=project_id)
        return ctx.call_tool(
            request,
            name="review.status",
            arguments={
                "project_id": project_id,
                "target_type": target_type,
                "target_id": target_id,
            },
        )

    @api_router.post("/api/projects/{project_id}/reviews/request", status_code=201)
    def request_review(
        project_id: str, request: Request, body: JsonBody = Body(default=None)
    ) -> dict[str, Any]:
        return ctx.call_tool(
            request,
            name="review.request",
            arguments=path_scoped_body(body, project_id=project_id),
        )

    @api_router.post("/api/projects/{project_id}/reviews/start")
    def start_review(
        project_id: str,
        request: Request,
        body: JsonBody = Body(default=None),
    ) -> dict[str, Any]:
        payload = body or {}
        api.app.reviews.assert_request_in_project(
            project_id=project_id,
            review_request_id=payload.get("review_request_id"),
        )
        return ctx.call_tool(
            request,
            name="review.start",
            arguments=payload,
            project_scope=project_id,
        )

    @api_router.post("/api/projects/{project_id}/reviews/submit")
    def submit_review(
        project_id: str, request: Request, body: JsonBody = Body(default=None)
    ) -> dict[str, Any]:
        payload = body or {}
        api.app.reviews.assert_session_in_project(
            project_id=project_id,
            review_session_id=payload.get("review_session_id"),
        )
        return ctx.call_tool(request, name="review.submit", arguments=payload)

    return api_router
