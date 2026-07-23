"""Literature review HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ....research_core.facade import ResearchLiterature
from .shared import conditional_json


def build_router(*, literature: ResearchLiterature) -> APIRouter:
    api_router = APIRouter()

    @api_router.get("/api/projects/{project_id}/litreview")
    def litreview(project_id: str, request: Request) -> Response:
        # The whole living review (summary, sections with cited papers, the
        # papers ledger with backlinks) behind a content-hash ETag so the
        # screen's conditional polling is cheap. UI-only read, no agent tool.
        return conditional_json(
            request, literature.ui_snapshot(project_id=project_id)
        )

    return api_router
