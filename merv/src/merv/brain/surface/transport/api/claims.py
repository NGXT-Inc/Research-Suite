"""Claims HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request

from ....kernel.utils import NotFoundError
from .shared import JsonBody, path_scoped_body

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()

    @api_router.get("/api/projects/{project_id}/claims")
    def list_claims(project_id: str, request: Request) -> dict[str, Any]:
        return ctx.call_tool(
            request, name="claim.list", arguments={"project_id": project_id}
        )

    @api_router.post("/api/projects/{project_id}/claims", status_code=201)
    def create_claim(
        project_id: str, request: Request, body: JsonBody = Body(default=None)
    ) -> dict[str, Any]:
        return ctx.call_tool(
            request,
            name="claim.create",
            arguments=path_scoped_body(body, project_id=project_id),
        )

    @api_router.get("/api/projects/{project_id}/claims/{claim_id}")
    def get_claim(project_id: str, claim_id: str, request: Request) -> dict[str, Any]:
        claims = ctx.call_tool(
            request, name="claim.list", arguments={"project_id": project_id}
        )["claims"]
        for claim in claims:
            if claim["id"] == claim_id:
                return claim
        raise NotFoundError(f"claim not found: {claim_id}")

    @api_router.patch("/api/projects/{project_id}/claims/{claim_id}")
    @api_router.put("/api/projects/{project_id}/claims/{claim_id}")
    def update_claim(
        project_id: str,
        claim_id: str,
        request: Request,
        body: JsonBody = Body(default=None),
    ) -> dict[str, Any]:
        return ctx.call_tool(
            request,
            name="claim.update",
            arguments=path_scoped_body(body, project_id=project_id, claim_id=claim_id),
        )

    return api_router
