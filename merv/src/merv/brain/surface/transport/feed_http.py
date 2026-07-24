"""Self-contained HTTP routes for the social feed (Feed_PRD.md).

The feed owns its routes so it stays a liftable module: nothing in the core UI
API (`http_api.py`) depends on it, and removing the feed is deleting the feed
package plus the single ``register_feed_routes`` call in ``create_fastapi_app``.

The registrar receives only the public Feed capability, project authorizer,
and activity sink used by these routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Query, Request
from fastapi.responses import JSONResponse, Response

from ...feed.facade import FeedDelivery
from ...kernel.utils import ValidationError
from .api.dependencies import ActivityTelemetry, AuthorizeProject

_TRACK_EVENTS = {"feed_opened", "post_viewed", "link_clicked", "image_viewed"}

# Only these analytics fields may ride along in a track payload. Everything else
# (ts, source, status, tool, args, result, project_id, …) is server-owned — a
# client must never be able to spread arbitrary keys into the activity log line.
_TRACK_PAYLOAD_FIELDS = {"post_id"}

# Feed images are agent-supplied bytes served same-origin; stop the browser from
# MIME-sniffing them into something executable.
_BASE_IMAGE_HEADERS = {"X-Content-Type-Options": "nosniff"}

# SVG is the one accepted image type that is also an active document: loaded via
# <img> it cannot script, but on DIRECT navigation a browser would run embedded
# <script>/on*= handlers (stored XSS). Serving it under a no-token `sandbox` plus
# `script-src 'none'` makes the document inert in every modern browser, so a
# first-party SVG chart is safe to accept. (External/unfurl SVGs never reach here
# — they are dropped raster-only upstream.)
_SVG_CONTENT_TYPE = "image/svg+xml"
_SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; sandbox"

# Feed embeds are interactive (scripted) HTML documents, so unlike SVG they
# need a permissive-but-isolated sandbox: scripts/styles run, but the sandbox
# token strips the document of same-origin, top navigation, popups, etc.
_EMBED_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data:"
)


def _image_headers(content_type: str) -> dict[str, str]:
    """Base hardening for every image, plus a CSP sandbox for SVG documents."""
    if (content_type or "").split(";", 1)[0].strip().lower() == _SVG_CONTENT_TYPE:
        return {**_BASE_IMAGE_HEADERS, "Content-Security-Policy": _SVG_CSP}
    return _BASE_IMAGE_HEADERS


def _too_large(cap: int) -> JSONResponse:
    return JSONResponse(
        {
            "detail": (
                f"upload exceeds the maximum of {cap} bytes for this feed post — "
                "slim the file (a feed visual is a single figure, not a dataset) "
                "and re-run the upload command"
            ),
            "error_code": "payload_too_large",
            "max_bytes": cap,
        },
        status_code=413,
    )


async def _read_capped(request: Request, *, cap: int) -> bytes | None:
    """Body bytes, or None once the cap is exceeded (never buffers past it)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        return None
    data = bytearray()
    async for chunk in request.stream():
        # INV-6: reject on projected size before extending so one oversized
        # ASGI chunk is never temporarily buffered past the token's cap.
        if len(data) + len(chunk) > cap:
            return None
        data.extend(chunk)
    return bytes(data)


def _enrich_post_urls(post: dict[str, Any], project_id: str) -> None:
    """Attach the relative media URLs the UI uses for <img src> (the service
    exposes only presence flags, never blob hashes)."""
    post_id = post.get("id")
    if post.get("has_image"):
        post["image_url"] = f"/api/projects/{project_id}/feed/{post_id}/image"
    if post.get("has_embed"):
        post["embed_url"] = f"/api/projects/{project_id}/feed/{post_id}/embed"
    preview = post.get("link_preview")
    if preview and preview.get("has_image"):
        preview["image_url"] = f"/api/projects/{project_id}/feed/{post_id}/link-image"


def register_feed_routes(
    http: Any,
    *,
    feed_api: FeedDelivery,
    authorize_project: AuthorizeProject,
    activity: ActivityTelemetry,
) -> None:
    """Register the feed's `/api/projects/{pid}/feed*` routes onto ``http``."""

    @http.put("/api/feed/u/{token}")
    async def upload_feed_media(token: str, request: Request) -> Any:
        # Auth-exempt (see RequestAuthenticator): the one-time token minted by
        # feed.post is the credential, so the agent's bare `curl -T` works
        # against both local and hosted brains. Token first (INV-12): an
        # unknown/used/expired token 404s before any body byte is buffered.
        cap = feed_api.pending_upload_cap(token=token)
        data = await _read_capped(request, cap=cap)
        if data is None:
            return _too_large(cap)
        try:
            return feed_api.complete_post_upload(token=token, data=data)
        except ValidationError as exc:
            if "max_bytes" in exc.details:
                return JSONResponse(
                    {"detail": exc.message, "error_code": "payload_too_large", **exc.details},
                    status_code=413,
                )
            raise

    @http.get("/api/projects/{project_id}/feed")
    def feed(
        request: Request,
        project_id: str,
        limit: int = Query(30, ge=1, le=100),
        cursor: int | None = Query(None),
    ) -> dict[str, Any]:
        authorize_project(request, project_id)
        result = feed_api.list_posts(
            project_id=project_id, limit=limit, before_seq=cursor
        )
        for post in result.get("posts", []):
            _enrich_post_urls(post, project_id)
        return result

    @http.post("/api/projects/{project_id}/feed/{post_id}/reactions")
    def feed_set_reaction(
        request: Request,
        project_id: str,
        post_id: str,
        body: Any = Body(default=None),
    ) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValidationError("reaction body must be a JSON object")
        authorize_project(request, project_id)
        result = feed_api.set_reaction(
            project_id=project_id,
            post_id=post_id,
            kind=str(body.get("kind") or ""),
            on=bool(body.get("on")),
        )
        if isinstance(result.get("post"), dict):
            _enrich_post_urls(result["post"], project_id)
        return result

    @http.post("/api/projects/{project_id}/feed/{post_id}/reply")
    def feed_reply(
        request: Request,
        project_id: str,
        post_id: str,
        body: Any = Body(default=None),
    ) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValidationError("reply body must be a JSON object")
        authorize_project(request, project_id)
        result = feed_api.researcher_reply(
            project_id=project_id,
            post_id=post_id,
            text=str(body.get("text") or ""),
        )
        if isinstance(result.get("post"), dict):
            _enrich_post_urls(result["post"], project_id)
        return result

    @http.get("/api/projects/{project_id}/feed/{post_id}/image")
    def feed_image(request: Request, project_id: str, post_id: str) -> Response:
        authorize_project(request, project_id)
        content, content_type = feed_api.get_image(
            project_id=project_id, post_id=post_id
        )
        return Response(
            content=content, media_type=content_type, headers=_image_headers(content_type)
        )

    @http.get("/api/projects/{project_id}/feed/{post_id}/link-image")
    def feed_link_image(request: Request, project_id: str, post_id: str) -> Response:
        authorize_project(request, project_id)
        content, content_type = feed_api.get_link_image(
            project_id=project_id, post_id=post_id
        )
        return Response(
            content=content, media_type=content_type, headers=_image_headers(content_type)
        )

    @http.get("/api/projects/{project_id}/feed/{post_id}/embed")
    def feed_embed(request: Request, project_id: str, post_id: str) -> Response:
        authorize_project(request, project_id)
        wrapped = feed_api.get_embed(
            project_id=project_id, post_id=post_id
        )
        return Response(
            content=wrapped,
            media_type="text/html; charset=utf-8",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": _EMBED_CSP,
            },
        )

    @http.post("/api/projects/{project_id}/feed/track")
    def feed_track(
        request: Request, project_id: str, body: Any = Body(default=None)
    ) -> dict[str, Any]:
        # Usage analytics (Feed_PRD.md). Recorded to the machine-local activity
        # log, NOT the domain event stream — so it never pollutes the Events
        # timeline nor inflates the posting-nudge signal.
        if not isinstance(body, dict):
            raise ValidationError("feed track body must be a JSON object")
        event = str(body.get("event") or "").strip()
        if event not in _TRACK_EVENTS:
            raise ValidationError(f"unknown feed event: {event!r}")
        # project_id comes from the (tenant-checked) URL only; the body may
        # contribute an explicit allowlist of analytics fields and nothing else,
        # so a caller cannot forge tool-call-shaped entries or retarget the
        # record at another tenant's project.
        authorize_project(request, project_id)
        activity.emit(
            event_type=f"feed.{event}",
            payload={
                "project_id": project_id,
                **{k: v for k, v in body.items() if k in _TRACK_PAYLOAD_FIELDS},
            },
        )
        return {"ok": True}
