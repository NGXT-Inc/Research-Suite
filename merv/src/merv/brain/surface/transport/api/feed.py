"""Feed route composition."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..feed_http import register_feed_routes
from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()

    def app_for_feed(project_id: str, request: Request):
        return ctx.api.app

    register_feed_routes(api_router, app_for=app_for_feed)
    return api_router
