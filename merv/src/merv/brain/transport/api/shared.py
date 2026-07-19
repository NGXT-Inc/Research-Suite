"""Shared HTTP API response helpers and CORS policy constants."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response

JsonBody = dict[str, Any] | None
UI_CORS_HEADERS = ["Content-Type", "Accept", "Authorization", "X-RP-Client-Version", "If-None-Match"]
# ETag is not CORS-safelisted; expose it so a cross-origin dev UI can echo it back.
UI_CORS_EXPOSE_HEADERS = ["ETag"]


def _json_body(payload: Any) -> bytes:
    """Serialize exactly like FastAPI's default JSON path for these handlers."""
    body = json.dumps(
        jsonable_encoder(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    return body


def _matches(request: Request, *, etag: str) -> bool:
    if_none_match = request.headers.get("if-none-match") or ""
    return etag in [tag.strip() for tag in if_none_match.split(",")]


def _signal_etag(*parts: object) -> str:
    raw = "\0".join(str(part) for part in parts).encode("utf-8")
    return f'"{hashlib.sha256(raw).hexdigest()[:32]}"'


def conditional_json(request: Request, payload: Any) -> Response:
    """ETag/304 wrapper for body-hash endpoints.

    Serializes exactly like FastAPI's default path (jsonable_encoder +
    compact json.dumps) so 200 bodies stay byte-identical for clients that
    never send If-None-Match; the ETag is a content hash of those bytes.
    """
    body = _json_body(payload)
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _matches(request, etag=etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def conditional_json_from_signal(
    request: Request,
    *,
    signal_parts: tuple[object, ...],
    payload: Callable[[], Any],
) -> Response:
    """ETag/304 wrapper for endpoints with a proven monotonic change signal."""
    etag = _signal_etag(*signal_parts)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _matches(request, etag=etag):
        return Response(status_code=304, headers=headers)
    return Response(content=_json_body(payload()), media_type="application/json", headers=headers)


def is_local_origin(origin: str) -> bool:
    host = (urllib.parse.urlsplit(origin).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")
