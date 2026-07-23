"""Owner-only HTTP lifecycle for project-scoped API keys.

Every route requires a Supabase browser session (``client_id`` starts with
``jwt:``): a project (``mk_``) key or an ``rr_sk_`` key cannot mint, list, or
revoke keys. Owner-minted keys carry the canonical ``/mcp`` audience when the
deployment configures one; unset leaves them un-audienced (Phase A has no
audience enforcement yet).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from ....kernel.utils import ValidationError
from ...identity import HumanSessionRequiredError
from ...project_keys import ProjectKeyControl
from .shared import JsonBody

# The only optional fields a JWT owner may supply when minting a key; audience
# is server-set. Anything else (e.g. a de-profiled ``profile``) is a 400 rather
# than a silently-ignored 201.
_CREATE_KEY_FIELDS = frozenset(
    {"expires_at", "parent_key_id", "sandbox_seconds_ceiling", "blob_bytes_ceiling"}
)


def build_router(*, keys: ProjectKeyControl, audience: str = "") -> APIRouter:
    router = APIRouter()
    owner_key_audience = (audience or "").strip() or None

    @router.post("/api/projects/{project_id}/keys", status_code=201)
    def create_project_key(
        project_id: str, request: Request, body: JsonBody = Body(default=None)
    ) -> dict[str, object]:
        payload = body or {}
        unknown = sorted(set(payload) - _CREATE_KEY_FIELDS)
        if unknown:
            raise ValidationError(
                "unsupported key-create field(s)", details={"fields": unknown}
            )
        return keys.create(
            project_id=project_id,
            owner_user_id=_owner(request),
            expires_at=payload.get("expires_at"),
            parent_key_id=payload.get("parent_key_id"),
            sandbox_seconds_ceiling=payload.get("sandbox_seconds_ceiling"),
            blob_bytes_ceiling=payload.get("blob_bytes_ceiling"),
            audience=owner_key_audience,
        )

    @router.get("/api/projects/{project_id}/keys")
    def list_project_keys(project_id: str, request: Request) -> dict[str, object]:
        return keys.list(project_id=project_id, owner_user_id=_owner(request))

    @router.post("/api/projects/{project_id}/keys/{key_id}/revoke")
    def revoke_project_key(
        project_id: str, key_id: str, request: Request
    ) -> dict[str, object]:
        return keys.revoke(
            project_id=project_id, key_id=key_id, owner_user_id=_owner(request)
        )

    return router


def _owner(request: Request) -> str:
    principal = request.state.principal
    if not str(getattr(principal, "client_id", "") or "").startswith("jwt:"):
        raise HumanSessionRequiredError(
            "project key management requires a Supabase browser session"
        )
    return str(getattr(principal, "user_id", "") or "")


__all__ = ["build_router"]
