"""Claims HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from .shared import JsonBody

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api
    @api_router.get("/api/projects/{project_id}/claims")
    def list_claims(project_id: str) -> dict[str, Any]:
        return api.call_tool(name="claim.list", arguments={"project_id": project_id})

    @api_router.post("/api/projects/{project_id}/claims", status_code=201)
    def create_claim(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api.call_tool(name="claim.create", arguments={"project_id": project_id, **(body or {})})

    @api_router.get("/api/projects/{project_id}/claims/{claim_id}")
    def get_claim(project_id: str, claim_id: str) -> dict[str, Any]:
        return api.get_claim(project_id=project_id, claim_id=claim_id)

    @api_router.patch("/api/projects/{project_id}/claims/{claim_id}")
    @api_router.put("/api/projects/{project_id}/claims/{claim_id}")
    def update_claim(project_id: str, claim_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api.call_tool(name="claim.update", arguments={"project_id": project_id, "claim_id": claim_id, **(body or {})})


    return api_router
