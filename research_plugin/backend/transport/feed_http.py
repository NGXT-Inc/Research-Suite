"""Self-contained HTTP routes for the social feed (Feed_PRD.md).

The feed owns its routes so it stays a liftable module: nothing in the core UI
API (`http_api.py`) depends on it, and removing the feed is deleting the feed
package plus the single ``register_feed_routes`` call in ``create_fastapi_app``.

The registrar takes an ``app_for(project_id)`` resolver (the same per-project
routing the rest of the API uses) and reads everything off ``app.feed`` — the
feed service is the only backend surface these routes touch.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Body, Query, Request
from fastapi.responses import Response

from ..utils import ValidationError

_TRACK_EVENTS = {"feed_opened", "post_viewed", "link_clicked", "image_viewed"}

# Only these analytics fields may ride along in a track payload. Everything else
# (ts, source, status, tool, args, result, project_id, …) is server-owned — a
# client must never be able to spread arbitrary keys into the activity log line.
_TRACK_PAYLOAD_FIELDS = {"post_id"}

# Feed images are user/agent-supplied bytes served same-origin; stop the browser
# from MIME-sniffing them into something executable (defense in depth alongside
# the SVG exclusion in FeedService).
_IMAGE_RESPONSE_HEADERS = {"X-Content-Type-Options": "nosniff"}


def register_feed_routes(
    http: Any, *, app_for: Callable[[str, Request], Any]
) -> None:
    """Register the feed's `/api/projects/{pid}/feed*` routes onto ``http``."""

    @http.get("/api/projects/{project_id}/feed")
    def feed(
        request: Request,
        project_id: str,
        limit: int = Query(30, ge=1, le=100),
        cursor: int | None = Query(None),
    ) -> dict[str, Any]:
        result = app_for(project_id, request).feed.list_posts(
            project_id=project_id, limit=limit, before_seq=cursor
        )
        # Enrich with the relative media URLs the UI uses for <img src> (the
        # service exposes only presence flags, never blob hashes).
        for post in result.get("posts", []):
            post_id = post.get("id")
            if post.get("has_image"):
                post["image_url"] = f"/api/projects/{project_id}/feed/{post_id}/image"
            preview = post.get("link_preview")
            if preview and preview.get("has_image"):
                preview["image_url"] = (
                    f"/api/projects/{project_id}/feed/{post_id}/link-image"
                )
        return result

    @http.get("/api/projects/{project_id}/feed/{post_id}/image")
    def feed_image(request: Request, project_id: str, post_id: str) -> Response:
        content, content_type = app_for(project_id, request).feed.get_image(
            project_id=project_id, post_id=post_id
        )
        return Response(
            content=content, media_type=content_type, headers=_IMAGE_RESPONSE_HEADERS
        )

    @http.get("/api/projects/{project_id}/feed/{post_id}/link-image")
    def feed_link_image(request: Request, project_id: str, post_id: str) -> Response:
        content, content_type = app_for(project_id, request).feed.get_link_image(
            project_id=project_id, post_id=post_id
        )
        return Response(
            content=content, media_type=content_type, headers=_IMAGE_RESPONSE_HEADERS
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
        app_for(project_id, request).activity.emit(
            event_type=f"feed.{event}",
            payload={
                "project_id": project_id,
                **{k: v for k, v in body.items() if k in _TRACK_PAYLOAD_FIELDS},
            },
        )
        return {"ok": True}
