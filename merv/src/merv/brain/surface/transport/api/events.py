"""Events HTTP routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response, StreamingResponse

from .shared import conditional_json_from_signal

from .context import ApiRouteContext


def build_router(ctx: ApiRouteContext) -> APIRouter:
    api_router = APIRouter()
    api = ctx.api
    @api_router.get("/api/projects/{project_id}/events")
    def events(project_id: str, request: Request, limit: int = Query(100, ge=1)) -> Response:
        target = api
        signal = target.app.store.project_event_signal(project_id=project_id)
        # Mirror the store's limit clamp so limit=501 and limit=502 share one
        # ETag (identical bodies must not cache-miss on token identity).
        effective_limit = max(1, min(int(limit), 500))
        return conditional_json_from_signal(
            request,
            signal_parts=("events", project_id, effective_limit, signal),
            payload=lambda: target.events(project_id=project_id, limit=limit),
        )

    @api_router.get("/api/projects/{project_id}/events/stream")
    def events_stream(
        project_id: str,
        request: Request,
        since: int | None = Query(None, ge=0),
        poll_ms: int = Query(1000, ge=100, le=5000),
        max_ms: int | None = Query(None, ge=100, le=3600_000),
    ) -> StreamingResponse:
        """Server push for the UI: tails the append-only events table over SSE.

        Emits `append` (one per event row, with SSE id for resume), `state`
        (per non-empty batch — the client's "run one refreshHome" signal), and
        comment keepalives. Cursor: ?since= wins, else Last-Event-ID (browser
        reconnect), else tail-only from the current head. `max_ms` bounds the
        session (the browser reconnects per the retry hint) — also what makes
        the stream finite for TestClient, which buffers whole responses.
        """
        store = api.app.store
        # Resolve the starting cursor eagerly so an unknown project 404s as
        # normal JSON instead of dying after SSE headers were sent.
        head = store.recent_events(project_id=project_id, limit=1)["events"]
        cursor = since
        if cursor is None:
            last_event_id = request.headers.get("last-event-id") or ""
            cursor = int(last_event_id) if last_event_id.isdigit() else None
        if cursor is None:
            cursor = int(head[0]["id"]) if head else 0

        def sse(event: str, data: Any) -> str:
            payload = json.dumps(jsonable_encoder(data), ensure_ascii=False, separators=(",", ":"))
            return f"event: {event}\ndata: {payload}\n\n"

        async def tail(start: int):
            import asyncio

            cursor = start
            yield f"retry: 3000\n{sse('hello', {'cursor': cursor})}"
            idle_ms = 0
            elapsed_ms = 0
            while True:
                batch = store.events_since(project_id=project_id, after_id=cursor)["events"]
                for row in batch:
                    cursor = int(row["id"])
                    yield f"id: {cursor}\n{sse('append', row)}"
                if batch:
                    idle_ms = 0
                    yield sse("state", {"version": cursor})
                else:
                    idle_ms += poll_ms
                    if idle_ms >= 15000:
                        idle_ms = 0
                        # A real event, not an SSE comment: comments never reach
                        # the EventSource JS API, and the client's liveness
                        # watchdog needs to see the heartbeat (a proxy can hold
                        # a dead upstream half-open without firing onerror).
                        yield sse("ping", {})
                if max_ms is not None and elapsed_ms >= max_ms:
                    return
                await asyncio.sleep(poll_ms / 1000)
                elapsed_ms += poll_ms

        return StreamingResponse(
            tail(cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


    return api_router
