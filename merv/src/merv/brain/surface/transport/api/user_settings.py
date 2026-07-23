"""Per-user, write-only secret settings over HTTP (no-dataplane Phase C).

The only setting today is the caller's own Hugging Face token. Every route
requires a Supabase browser session (``client_id`` starts with ``jwt:``): a
project (``mk_``) key or an ``rr_sk_`` key cannot set another user's secret, and
there is deliberately NO read route — the value is write-only and surfaces only
internally at sandbox provisioning.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from ...identity import HumanSessionRequiredError
from ...user_settings import UserHfTokenSettings
from .shared import JsonBody


def build_router(*, user_settings: UserHfTokenSettings) -> APIRouter:
    router = APIRouter()

    @router.put("/api/user/hf-token")
    def set_hf_token(
        request: Request, body: JsonBody = Body(default=None)
    ) -> dict[str, object]:
        payload = body or {}
        return user_settings.set_token(
            user_id=_owner(request), token=str(payload.get("token") or "")
        )

    @router.delete("/api/user/hf-token")
    def clear_hf_token(request: Request) -> dict[str, object]:
        return user_settings.clear_token(user_id=_owner(request))

    return router


def _owner(request: Request) -> str:
    principal = request.state.principal
    if not str(getattr(principal, "client_id", "") or "").startswith("jwt:"):
        raise HumanSessionRequiredError(
            "setting a personal Hugging Face token requires a Supabase browser session"
        )
    return str(getattr(principal, "user_id", "") or "")


__all__ = ["build_router"]
