"""Browser-handoff device flow for CLI/proxy sign-in (hosted auth only).

The RapidReview SDK contract, reimplemented against the same Supabase project:
`merv-client login` mints a session here, the user signs in on the web UI's
/auth/sdk page (supabase-js), the page posts the session tokens back, and the
CLI polls until they arrive. Refresh is proxied so clients never talk to
Supabase directly. Registered only when the app has an auth verifier; the
local surface has none of these routes.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from ...utils import ValidationError
from ...services.auth import UnauthorizedError
from .shared import JsonBody

SESSION_TTL_SECONDS = 600.0
_POLL_PENDING = {"status": "pending"}


def build_router(
    *, verifier: Any, allowed_origins: list[str], ui_base_url: str = ""
) -> APIRouter:
    router = APIRouter()
    sessions: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def _sweep() -> None:
        deadline = time.time() - SESSION_TTL_SECONDS
        for sid in [s for s, row in sessions.items() if row["created"] < deadline]:
            sessions.pop(sid, None)

    def _ui_origin(request: Request) -> str:
        # The sign-in page lives on the hosted UI: the configured UI base URL
        # (may carry a path, e.g. /merv) wins, else the first CORS origin when
        # the UI is served cross-origin, else this server's own origin.
        if ui_base_url:
            return ui_base_url
        if allowed_origins:
            return allowed_origins[0].rstrip("/")
        return str(request.base_url).rstrip("/")

    @router.post("/api/sdk/auth/session")
    def create_session(request: Request) -> dict[str, Any]:
        session_id = secrets.token_urlsafe(24)
        with lock:
            _sweep()
            sessions[session_id] = {"created": time.time(), "status": "pending"}
        return {
            "session_id": session_id,
            "auth_url": f"{_ui_origin(request)}/auth/sdk?session={session_id}",
        }

    @router.post("/api/sdk/auth/session/complete")
    def complete_session(body: JsonBody = Body(default=None)) -> dict[str, Any]:
        payload = body or {}
        session_id = str(payload.get("session_id") or "")
        access_token = str(payload.get("access_token") or "")
        # Never store tokens the verifier would reject: the browser proves the
        # session is real before the CLI ever sees it.
        try:
            verifier.verify_bearer(f"Bearer {access_token}")
        except UnauthorizedError as exc:
            return JSONResponse(
                {"detail": exc.message, "error_code": "unauthorized"}, status_code=401
            )
        with lock:
            _sweep()
            row = sessions.get(session_id)
            if row is None or row["status"] != "pending":
                raise ValidationError(
                    "unknown or expired login session; rerun merv-client login",
                    details={"field": "session_id"},
                )
            row.update(
                status="complete",
                tokens={
                    "access_token": access_token,
                    "refresh_token": str(payload.get("refresh_token") or ""),
                    "expires_in": int(payload.get("expires_in") or 3600),
                    "email": str(payload.get("email") or ""),
                },
            )
        return {"ok": True}

    @router.post("/api/sdk/auth/session/poll")
    def poll_session(body: JsonBody = Body(default=None)) -> dict[str, Any]:
        session_id = str((body or {}).get("session_id") or "")
        with lock:
            _sweep()
            row = sessions.get(session_id)
            if row is None:
                raise ValidationError(
                    "unknown or expired login session; rerun merv-client login",
                    details={"field": "session_id"},
                )
            if row["status"] != "complete":
                return dict(_POLL_PENDING)
            # One-shot handoff: the tokens leave the store with this response.
            sessions.pop(session_id, None)
            return {"status": "complete", **row["tokens"]}

    @router.post("/api/sdk/auth/refresh")
    def refresh(body: JsonBody = Body(default=None)) -> dict[str, Any]:
        refresh_token = str((body or {}).get("refresh_token") or "")
        if not refresh_token:
            raise ValidationError("refresh_token is required", details={"field": "refresh_token"})
        try:
            return verifier.refresh_session(refresh_token)
        except UnauthorizedError as exc:
            return JSONResponse(
                {"detail": exc.message, "error_code": "unauthorized"}, status_code=401
            )

    return router
