"""Bounded request-body streaming shared by public HTTP adapters."""

from __future__ import annotations

from fastapi import Request


class RequestBodyTooLarge(Exception):
    def __init__(self, *, limit: int) -> None:
        super().__init__(f"request body exceeds {limit} bytes")
        self.limit = int(limit)


async def read_limited_body(request: Request, *, limit: int) -> bytes:
    """Read at most ``limit`` bytes without calling Starlette's body buffer."""
    declared = request.headers.get("Content-Length", "").strip()
    if declared:
        try:
            if int(declared) > limit:
                raise RequestBodyTooLarge(limit=limit)
        except ValueError:
            pass
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise RequestBodyTooLarge(limit=limit)
        body.extend(chunk)
    return bytes(body)


__all__ = ["RequestBodyTooLarge", "read_limited_body"]
