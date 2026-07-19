"""Social feed service (Feed_PRD.md).

The feed is the platform's one *informal* visibility surface: agents post brief,
curated aha-moments for the human to glance at, in reverse-chronological order.
Unlike every other agent-output surface it is editorial rather than complete,
ungated rather than reviewed, and may carry intuition not tied to any state
change — so posts are their own append-only, immutable entity (no edit/delete; a
correction is a new post), not events and not resources.

This service owns three agent tools — ``feed.register`` (claim a sci-fi handle),
``feed.post`` (write), ``feed.list`` (read back) — plus the read/image views the
UI consumes and the soft posting nudge surfaced through ``workflow``.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from . import feed_policy
from .feed_embeds import MAX_FEED_EMBED_BYTES, sniff_html_type, wrap_embed_html
from .feed_images import (
    MAX_FEED_IMAGE_BYTES,
    SERVEABLE_IMAGE_TYPES,
    sniff_image_type,
)
from ..kernel.state.store import BaseStateStore, next_created_seq, row_to_dict, rows_to_dicts
from ..kernel.utils import NotFoundError, ValidationError, new_id, now_iso, parse_iso
from .feed_unfurl import UnfurlError, fetch_preview_image, unfurl

# Hard cap on post text — "old Twitter, not an essay" (Feed_PRD.md open question,
# resolved to a hard cap). Counted on the stripped string.
POST_TEXT_MAX = 280

AUTHOR_ROLES = frozenset({"main", "reviewer", "lens", "researcher"})

# Optional editorial kind, self-declared by the posting agent (never inferred
# from text). Drives the type accent in the UI. `status` marks a mid-run
# checkpoint in a live experiment thread (distinct from `finding`, which is a
# landed result).
POST_KINDS = frozenset(
    {"finding", "hunch", "bottleneck", "kill", "direction", "status"}
)

# Human -> agent feedback: exactly these three, single-researcher product so
# counts would be meaningless — the researcher either has or hasn't set one.
REACTION_KINDS = frozenset({"fire", "eyes", "question"})

# Backward-compatible import surface for HTTP code/tests.
MAX_IMAGE_BYTES = MAX_FEED_IMAGE_BYTES
MAX_EMBED_BYTES = MAX_FEED_EMBED_BYTES

RESEARCHER_HANDLE = "Researcher"

_KNOWN_REF_PREFIXES = ("exp_", "claim_", "res_", "rver_", "syn_", "rev_")

# The feed owns its schema so it stays a liftable module rather than living in
# the shared store SCHEMA constant. The DDL is dialect-neutral (only TEXT/INTEGER,
# no AUTOINCREMENT/PRAGMA), so it runs unchanged on both SQLite (local/daemon) and
# the Postgres control plane. Created idempotently when FeedService is built.
FEED_SCHEMA: tuple[str, ...] = (
    # Posts are append-only and immutable (no edit/delete — a correction is a new
    # post). A post is editorial, not a state mutation, so it lives here rather
    # than in `events`; its optional `ref` to a domain entity may be empty
    # (un-anchored intuition). Image bytes and any re-hosted link thumbnail live
    # in the blob store keyed by sha256; the row carries only the reference.
    """
    CREATE TABLE IF NOT EXISTS posts (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      author_handle TEXT NOT NULL DEFAULT '',
      author_role TEXT NOT NULL DEFAULT 'main',
      text TEXT NOT NULL DEFAULT '',
      image_sha256 TEXT NOT NULL DEFAULT '',
      image_content_type TEXT NOT NULL DEFAULT '',
      link_url TEXT NOT NULL DEFAULT '',
      link_preview_json TEXT NOT NULL DEFAULT '{}',
      ref TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      created_seq INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY(project_id) REFERENCES projects(id)
    )
    """,
    # An agent registers a self-chosen sci-fi handle when it logs on, so parallel
    # agents post under distinct voices. The handle is unique per project. `role`
    # is captured so only main agents are ever nudged (reviewers/lens agents may
    # post, never prompted).
    """
    CREATE TABLE IF NOT EXISTS feed_authors (
      project_id TEXT NOT NULL,
      handle TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'main',
      session_id TEXT NOT NULL DEFAULT '',
      registered_at TEXT NOT NULL,
      last_posted_at TEXT,
      PRIMARY KEY (project_id, handle),
      FOREIGN KEY(project_id) REFERENCES projects(id)
    )
    """,
    # Human -> agent feedback. Single-researcher product, so there is no actor
    # column: a reaction kind is either set for the post or it isn't, hence the
    # kind is part of the primary key rather than a countable row.
    """
    CREATE TABLE IF NOT EXISTS post_reactions (
      project_id TEXT NOT NULL,
      post_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (project_id, post_id, kind),
      FOREIGN KEY(project_id) REFERENCES projects(id)
    )
    """,
)


def _validate_handle(handle: str) -> str:
    handle = (handle or "").strip()
    if not handle:
        raise ValidationError("handle is required")
    if len(handle) < 2 or len(handle) > 40:
        raise ValidationError("handle must be 2-40 characters")
    allowed = set(" -_.")
    if not all(ch.isalnum() or ch in allowed for ch in handle):
        raise ValidationError(
            "handle may use letters, digits, spaces, and - _ . only"
        )
    return handle


class FeedService:
    def __init__(self, *, store: BaseStateStore, blobs: Any) -> None:
        self.store = store
        self.blobs = blobs
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the feed's own tables (idempotent)."""
        with self.store.transaction() as conn:
            for statement in FEED_SCHEMA:
                conn.execute(statement)
        # Columns added after first ship. Each runs in its own transaction (a
        # failed ALTER aborts the whole transaction on Postgres) and failure
        # means the column already exists — both dialects lack a portable
        # IF NOT EXISTS for columns.
        try:
            with self.store.transaction() as conn:
                conn.execute("ALTER TABLE posts ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001
            pass
        try:
            with self.store.transaction() as conn:
                conn.execute(
                    "ALTER TABLE posts ADD COLUMN in_reply_to TEXT NOT NULL DEFAULT ''"
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            with self.store.transaction() as conn:
                conn.execute(
                    "ALTER TABLE posts ADD COLUMN embed_sha256 TEXT NOT NULL DEFAULT ''"
                )
        except Exception:  # noqa: BLE001
            pass
        try:
            with self.store.transaction() as conn:
                conn.execute(
                    "ALTER TABLE posts ADD COLUMN embed_content_type TEXT NOT NULL DEFAULT ''"
                )
        except Exception:  # noqa: BLE001
            pass

    # -- identity -----------------------------------------------------------

    def register(
        self,
        *,
        handle: str,
        role: str = "main",
        session_id: str = "",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim a self-chosen handle for this project (idempotent per session).

        A handle is unique per project so parallel agents post under distinct
        voices. Re-registering the same handle from the same session is a no-op;
        a different session claiming a live handle is rejected so two agents do
        not collide on one name.
        """
        handle = _validate_handle(handle)
        if role not in AUTHOR_ROLES:
            raise ValidationError(
                f"unknown author role: {role}. Allowed: {', '.join(sorted(AUTHOR_ROLES))}"
            )
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            existing = conn.execute(
                "SELECT * FROM feed_authors WHERE project_id = ? AND handle = ?",
                (project_id, handle),
            ).fetchone()
            if existing is not None:
                if (
                    existing["session_id"]
                    and session_id
                    and existing["session_id"] != session_id
                ):
                    raise ValidationError(
                        f"handle '{handle}' is already in use in this project; "
                        "choose another sci-fi name"
                    )
                conn.execute(
                    "UPDATE feed_authors SET role = ?, session_id = ? WHERE project_id = ? AND handle = ?",
                    (role, session_id or existing["session_id"], project_id, handle),
                )
                row = conn.execute(
                    "SELECT * FROM feed_authors WHERE project_id = ? AND handle = ?",
                    (project_id, handle),
                ).fetchone()
                return {"author": row_to_dict(row=row), "created": False}
            conn.execute(
                """
                INSERT INTO feed_authors (project_id, handle, role, session_id, registered_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, handle, role, session_id, now_iso()),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="feed.author_registered",
                target_type="feed_author",
                target_id=handle,
                payload={"handle": handle, "role": role},
            )
            row = conn.execute(
                "SELECT * FROM feed_authors WHERE project_id = ? AND handle = ?",
                (project_id, handle),
            ).fetchone()
            return {"author": row_to_dict(row=row), "created": True}

    # -- writing ------------------------------------------------------------

    def post(
        self,
        *,
        handle: str,
        text: str,
        image_path: str | None = None,
        html_path: str | None = None,
        url: str | None = None,
        ref: str | None = None,
        kind: str | None = None,
        in_reply_to: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Write a post. ``handle`` must already be registered in this project."""
        if image_path:
            raise ValidationError(
                "image_path must be read by the local data plane before posting"
            )
        if html_path:
            raise ValidationError(
                "html_path must be read by the local data plane before posting"
            )
        return self._post(
            handle=handle,
            text=text,
            image_path=None,
            image_bytes=None,
            html_path=None,
            html_bytes=None,
            url=url,
            ref=ref,
            kind=kind,
            in_reply_to=in_reply_to,
            project_id=project_id,
        )

    def post_observed(
        self,
        *,
        handle: str,
        text: str,
        image_path: str | None = None,
        image_bytes: bytes | None = None,
        html_path: str | None = None,
        html_bytes: bytes | None = None,
        url: str | None = None,
        ref: str | None = None,
        kind: str | None = None,
        in_reply_to: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Write a post from daemon-submitted local image/embed bytes."""
        return self._post(
            handle=handle,
            text=text,
            image_path=image_path,
            image_bytes=image_bytes,
            html_path=html_path,
            html_bytes=html_bytes,
            url=url,
            ref=ref,
            kind=kind,
            in_reply_to=in_reply_to,
            project_id=project_id,
        )

    def validate_post_intent(
        self,
        *,
        handle: str,
        text: str,
        ref: str | None = None,
        kind: str | None = None,
        in_reply_to: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate feed post metadata without reading/storing image bytes."""
        handle, text, ref, kind = self._validate_post_fields(
            handle=handle, text=text, ref=ref, kind=kind
        )
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            author = conn.execute(
                "SELECT role FROM feed_authors WHERE project_id = ? AND handle = ?",
                (project_id, handle),
            ).fetchone()
            if author is None:
                raise ValidationError(
                    f"handle '{handle}' is not registered; call feed.register first"
                )
            in_reply_to = self._validate_in_reply_to(
                conn=conn, project_id=project_id, in_reply_to=in_reply_to
            )
            return {
                "ok": True,
                "project_id": project_id,
                "handle": handle,
                "text": text,
                "ref": ref,
                "kind": kind,
                "in_reply_to": in_reply_to or None,
                "author_role": str(author["role"] or "main"),
            }

    def _post(
        self,
        *,
        handle: str,
        text: str,
        image_path: str | None,
        image_bytes: bytes | None,
        html_path: str | None = None,
        html_bytes: bytes | None = None,
        url: str | None,
        ref: str | None,
        kind: str | None,
        in_reply_to: str | None = None,
        project_id: str | None,
    ) -> dict[str, Any]:
        handle, text, ref, kind = self._validate_post_fields(
            handle=handle, text=text, ref=ref, kind=kind
        )
        if image_bytes is not None and html_bytes is not None:
            raise ValidationError("a post may carry an image or an embed, not both")
        # Resolve the project and author first (fail fast), then do the slow
        # work — image blob writes and link unfurling (network I/O) — with no
        # transaction open: the single-writer lock must never be held across
        # network calls (a slow unfurl would stall every other writer).
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            author = conn.execute(
                "SELECT role FROM feed_authors WHERE project_id = ? AND handle = ?",
                (project_id, handle),
            ).fetchone()
            if author is None:
                raise ValidationError(
                    f"handle '{handle}' is not registered; call feed.register first"
                )
            author_role = str(author["role"] or "main")
            in_reply_to = self._validate_in_reply_to(
                conn=conn, project_id=project_id, in_reply_to=in_reply_to
            )

        image_sha256 = ""
        image_content_type = ""
        if image_bytes is not None:
            image_sha256, image_content_type = self._capture_image_bytes(
                project_id=project_id,
                image_path=image_path or "feed-image",
                data=image_bytes,
            )
        elif image_path:
            raise ValidationError(
                "image bytes are required when image_path is provided"
            )

        embed_sha256 = ""
        embed_content_type = ""
        if html_bytes is not None:
            embed_sha256, embed_content_type = self._capture_embed_bytes(
                project_id=project_id,
                html_path=html_path or "feed-embed",
                data=html_bytes,
            )
        elif html_path:
            raise ValidationError(
                "embed bytes are required when html_path is provided"
            )

        link_url = ""
        link_preview: dict[str, Any] = {}
        if url:
            link_url, link_preview = self._build_link_preview(
                project_id=project_id, url=url
            )

        with self.store.transaction() as conn:
            post_id = new_id(prefix="post")
            created_at = now_iso()
            seq = next_created_seq(conn=conn, table="posts")
            conn.execute(
                """
                INSERT INTO posts (
                    id, project_id, author_handle, author_role, text,
                    image_sha256, image_content_type, link_url, link_preview_json,
                    ref, kind, in_reply_to, embed_sha256, embed_content_type,
                    created_at, created_seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    project_id,
                    handle,
                    author_role,
                    text,
                    image_sha256,
                    image_content_type,
                    link_url,
                    json.dumps(link_preview, sort_keys=True),
                    ref,
                    kind,
                    in_reply_to,
                    embed_sha256,
                    embed_content_type,
                    created_at,
                    seq,
                ),
            )
            conn.execute(
                "UPDATE feed_authors SET last_posted_at = ? WHERE project_id = ? AND handle = ?",
                (created_at, project_id, handle),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="feed.post_created",
                target_type="post",
                target_id=post_id,
                payload={
                    "handle": handle,
                    "has_image": bool(image_sha256),
                    "has_embed": bool(embed_sha256),
                    "has_link": bool(link_url),
                    "ref": ref,
                },
            )
            row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
            return {"post": self._post_view(row_to_dict(row=row) or {}, conn=conn)}

    def researcher_reply(
        self, *, post_id: str, text: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Post a researcher reply threaded under ``post_id``.

        Auto-registers the fixed "Researcher" handle idempotently — the human
        does not go through feed.register.
        """
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            existing = conn.execute(
                "SELECT 1 FROM feed_authors WHERE project_id = ? AND handle = ?",
                (project_id, RESEARCHER_HANDLE),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO feed_authors (project_id, handle, role, session_id, registered_at)
                    VALUES (?, ?, 'researcher', '', ?)
                    """,
                    (project_id, RESEARCHER_HANDLE, now_iso()),
                )
        return self._post(
            handle=RESEARCHER_HANDLE,
            text=text,
            image_path=None,
            image_bytes=None,
            html_path=None,
            html_bytes=None,
            url=None,
            ref=None,
            kind=None,
            in_reply_to=post_id,
            project_id=project_id,
        )

    def _validate_in_reply_to(
        self, *, conn: Any, project_id: str, in_reply_to: str | None
    ) -> str:
        in_reply_to = (in_reply_to or "").strip()
        if not in_reply_to:
            return ""
        target = conn.execute(
            "SELECT 1 FROM posts WHERE id = ? AND project_id = ?",
            (in_reply_to, project_id),
        ).fetchone()
        if target is None:
            raise ValidationError(f"in_reply_to post not found: {in_reply_to}")
        return in_reply_to

    def _validate_post_fields(
        self, *, handle: str, text: str, ref: str | None, kind: str | None = None
    ) -> tuple[str, str, str, str]:
        handle = _validate_handle(handle)
        text = (text or "").strip()
        if not text:
            raise ValidationError("post text is required")
        if len(text) > POST_TEXT_MAX:
            raise ValidationError(
                f"post text is {len(text)} chars; keep it under {POST_TEXT_MAX} "
                "(brief, like old Twitter — this is not the place for an essay)"
            )
        ref = (ref or "").strip()
        if ref and not ref.startswith(_KNOWN_REF_PREFIXES):
            raise ValidationError(
                "ref must point at a project entity "
                f"({', '.join(p.rstrip('_') for p in _KNOWN_REF_PREFIXES)})"
            )
        kind = (kind or "").strip().lower()
        if kind and kind not in POST_KINDS:
            raise ValidationError(
                f"unknown post kind: {kind}. Allowed: {', '.join(sorted(POST_KINDS))} (or omit it)"
            )
        return handle, text, ref, kind

    def _capture_image_bytes(
        self, *, project_id: str, image_path: str, data: bytes
    ) -> tuple[str, str]:
        """Store already-read image bytes, returning (sha, content_type)."""
        if len(data) > MAX_IMAGE_BYTES:
            raise ValidationError(
                f"image is {len(data)} bytes; keep feed images under {MAX_IMAGE_BYTES}"
            )
        candidate = Path(image_path or "feed-image")
        content_type = sniff_image_type(candidate, data)
        if content_type is None:
            raise ValidationError(
                f"{image_path} does not look like an image (png/jpeg/gif/webp/svg)"
            )
        sha = self.blobs.put(namespace=project_id, data=data)
        return sha, content_type

    def _capture_embed_bytes(
        self, *, project_id: str, html_path: str, data: bytes
    ) -> tuple[str, str]:
        """Store already-read HTML embed bytes, returning (sha, content_type)."""
        if len(data) > MAX_EMBED_BYTES:
            raise ValidationError(
                f"embed is {len(data)} bytes; keep feed embeds under {MAX_EMBED_BYTES}"
            )
        content_type = sniff_html_type(data)
        if content_type is None:
            raise ValidationError(f"{html_path} does not look like an HTML document")
        sha = self.blobs.put(namespace=project_id, data=data)
        return sha, content_type

    def _build_link_preview(
        self, *, project_id: str, url: str
    ) -> tuple[str, dict[str, Any]]:
        """Unfurl ``url`` into a static preview; degrade to a plain link on failure.

        Per the PRD edge case, a bad or disallowed link never fails the post — it
        becomes a plain, non-embedded chip (``preview.error`` set). Exception:
        a non-web scheme (javascript:/data:/file:…) is attacker-shaped, not
        degradable — the post survives, but nothing clickable is stored.
        """
        url = url.strip()
        if urllib.parse.urlparse(url).scheme.lower() not in ("http", "https"):
            return "", {"url": "", "error": "only http and https links can be embedded"}
        try:
            card = unfurl(url)
        except UnfurlError as exc:
            return url, {"url": url, "error": str(exc)}
        preview: dict[str, Any] = {
            "url": card["url"],
            "title": card.get("title", ""),
            "description": card.get("description", ""),
            "trusted": bool(card.get("trusted")),
            "kind": card.get("kind") or "page",
            "authors": card.get("authors") or [],
            "year": card.get("year") or "",
        }
        image_url = card.get("image_url") or ""
        if image_url:
            try:
                img_bytes, ctype = fetch_preview_image(image_url)
                normalized = (ctype or "").split(";", 1)[0].strip().lower()
                # Only re-host raster thumbnails. An external SVG og:image would
                # otherwise be served same-origin (stored XSS); drop it to a
                # text-only card instead.
                if normalized in SERVEABLE_IMAGE_TYPES:
                    preview["image_sha256"] = self.blobs.put(
                        namespace=project_id, data=img_bytes
                    )
                    preview["image_content_type"] = normalized
            except UnfurlError:
                # A missing/unsafe thumbnail just means a text-only preview card.
                pass
        return url, preview

    # -- reading ------------------------------------------------------------

    def list_posts(
        self,
        *,
        project_id: str | None = None,
        limit: int = 30,
        before_seq: int | None = None,
    ) -> dict[str, Any]:
        """Reverse-chronological posts, cursor-paginated by ``created_seq``."""
        limit = max(1, min(int(limit), 100))
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            params: list[Any] = [project_id]
            where = "project_id = ?"
            if before_seq is not None:
                where += " AND created_seq < ?"
                params.append(int(before_seq))
            params.append(limit + 1)
            rows = conn.execute(
                f"SELECT * FROM posts WHERE {where} ORDER BY created_seq DESC LIMIT ?",
                params,
            ).fetchall()
            items = rows_to_dicts(rows=rows)
            has_more = len(items) > limit
            items = items[:limit]
            next_cursor = items[-1]["created_seq"] if (has_more and items) else None
            views = [self._post_view(item, conn=conn) for item in items]
            result: dict[str, Any] = {
                "posts": views,
                "next_cursor": next_cursor,
            }
            # On the first page (an agent reading the feed), include the soft
            # posting nudge if one applies. This is how the backup cadence signal
            # reaches the agent — through the feed's own surface, so the core
            # research workflow has no dependency on the feed.
            if before_seq is None:
                nudge = self.feed_nudge(project_id=project_id, conn=conn)
                if nudge is not None:
                    result["nudge"] = nudge
                # Surface what the researcher has reacted to, most recent first,
                # so an agent reading page 1 sees it without a separate call.
                attention = [
                    {
                        "post_id": view["id"],
                        "reactions": [
                            kind for kind, on in view["reactions"].items() if on
                        ],
                        "text_snippet": (view["text"] or "")[:80],
                    }
                    for view in views
                    if any(view["reactions"].values())
                ][:5]
                if attention:
                    result["researcher_attention"] = attention
            return result
        finally:
            conn.close()

    def get_embed(self, *, project_id: str, post_id: str) -> str:
        """Return the CSP-wrapped HTML document for a post's embed."""
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                "SELECT embed_sha256 FROM posts WHERE id = ? AND project_id = ?",
                (post_id, project_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["embed_sha256"]:
            raise NotFoundError(f"no embed for post: {post_id}")
        data = self.blobs.get(namespace=project_id, sha256=str(row["embed_sha256"]))
        return wrap_embed_html(data)

    def get_image(self, *, project_id: str, post_id: str) -> tuple[bytes, str]:
        """Return (bytes, content_type) for a post's image, for the HTTP route."""
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                "SELECT image_sha256, image_content_type FROM posts WHERE id = ? AND project_id = ?",
                (post_id, project_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["image_sha256"]:
            raise NotFoundError(f"no image for post: {post_id}")
        data = self.blobs.get(namespace=project_id, sha256=str(row["image_sha256"]))
        return data, str(row["image_content_type"] or "application/octet-stream")

    def get_link_image(self, *, project_id: str, post_id: str) -> tuple[bytes, str]:
        """Return (bytes, content_type) for a post's re-hosted link thumbnail."""
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                "SELECT link_preview_json FROM posts WHERE id = ? AND project_id = ?",
                (post_id, project_id),
            ).fetchone()
        finally:
            conn.close()
        sha = ""
        ctype = ""
        if row is not None:
            try:
                preview = json.loads(row["link_preview_json"] or "{}")
                sha = str(preview.get("image_sha256") or "")
                ctype = str(preview.get("image_content_type") or "")
            except (TypeError, ValueError):
                sha = ""
        if not sha:
            raise NotFoundError(f"no link image for post: {post_id}")
        # Serve the real sniffed content type captured at unfurl time. Older rows
        # predate the stored type; fall back to a safe non-renderable default
        # rather than the invalid `image/*` media range.
        return (
            self.blobs.get(namespace=project_id, sha256=sha),
            ctype or "application/octet-stream",
        )

    def set_reaction(
        self, *, post_id: str, kind: str, on: bool, project_id: str | None = None
    ) -> dict[str, Any]:
        """Idempotently set/clear a researcher reaction, returning the post view."""
        kind = (kind or "").strip().lower()
        if kind not in REACTION_KINDS:
            raise ValidationError(
                f"unknown reaction kind: {kind}. Allowed: {', '.join(sorted(REACTION_KINDS))}"
            )
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                "SELECT * FROM posts WHERE id = ? AND project_id = ?",
                (post_id, project_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"post not found: {post_id}")
            if on:
                conn.execute(
                    """
                    INSERT INTO post_reactions (project_id, post_id, kind, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_id, post_id, kind) DO NOTHING
                    """,
                    (project_id, post_id, kind, now_iso()),
                )
            else:
                conn.execute(
                    "DELETE FROM post_reactions WHERE project_id = ? AND post_id = ? AND kind = ?",
                    (project_id, post_id, kind),
                )
            row = conn.execute(
                "SELECT * FROM posts WHERE id = ? AND project_id = ?",
                (post_id, project_id),
            ).fetchone()
            return {"post": self._post_view(row_to_dict(row=row) or {}, conn=conn)}

    def _post_view(self, item: dict[str, Any], *, conn: Any) -> dict[str, Any]:
        preview_raw = item.get("link_preview_json") or "{}"
        try:
            link_preview = json.loads(preview_raw)
        except (TypeError, ValueError):
            link_preview = {}
        clean_preview: dict[str, Any] | None = None
        if link_preview:
            # Don't leak the blob hash to clients — expose only presence.
            # kind/authors/year default sanely for rows unfurled before they existed.
            clean_preview = {
                "url": link_preview.get("url"),
                "title": link_preview.get("title") or "",
                "description": link_preview.get("description") or "",
                "trusted": bool(link_preview.get("trusted")),
                "has_image": bool(link_preview.get("image_sha256")),
                "error": link_preview.get("error"),
                "kind": link_preview.get("kind") or "page",
                "authors": link_preview.get("authors") or [],
                "year": link_preview.get("year") or "",
            }
        return {
            "id": item.get("id"),
            "author_handle": item.get("author_handle"),
            "author_role": item.get("author_role"),
            "text": item.get("text"),
            "ref": item.get("ref") or None,
            "kind": item.get("kind") or None,
            "in_reply_to": item.get("in_reply_to") or None,
            "has_image": bool(item.get("image_sha256")),
            "has_embed": bool(item.get("embed_sha256")),
            "link_url": item.get("link_url") or None,
            "link_preview": clean_preview,
            "reactions": self._reactions_for_post(
                conn=conn, project_id=str(item.get("project_id") or ""), post_id=str(item.get("id") or "")
            ),
            "created_at": item.get("created_at"),
            "created_seq": item.get("created_seq"),
        }

    def _reactions_for_post(
        self, *, conn: Any, project_id: str, post_id: str
    ) -> dict[str, bool]:
        rows = conn.execute(
            "SELECT kind FROM post_reactions WHERE project_id = ? AND post_id = ?",
            (project_id, post_id),
        ).fetchall()
        on = {str(row["kind"]) for row in rows}
        return {kind: kind in on for kind in sorted(REACTION_KINDS)}

    # -- posting nudge (backup cadence signal) ------------------------------

    def feed_signal(self, *, project_id: str, conn: Any) -> dict[str, Any]:
        """Raw cadence numbers: events and hours since the last AGENT post.

        A researcher reply is not agent activity — it must not reset the
        cold-feed clock, or a human commenting on an old post would silence
        the nudge without the agent having posted anything new.
        """
        last = conn.execute(
            "SELECT created_at FROM posts WHERE project_id = ? AND author_role <> 'researcher' "
            "ORDER BY created_seq DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        last_post_at = last["created_at"] if last is not None else None
        # Count real research activity, not the feed's own events — feed.* rows
        # (post_created, author_registered, UI telemetry) must not nudge the agent
        # to post just because it already posted.
        if last_post_at:
            events_since = conn.execute(
                "SELECT COUNT(*) AS n FROM events "
                "WHERE project_id = ? AND created_at > ? AND substr(type, 1, 5) <> 'feed.'",
                (project_id, last_post_at),
            ).fetchone()["n"]
        else:
            events_since = conn.execute(
                "SELECT COUNT(*) AS n FROM events "
                "WHERE project_id = ? AND substr(type, 1, 5) <> 'feed.'",
                (project_id,),
            ).fetchone()["n"]
        hours_since = _hours_since(last_post_at)
        return {
            "last_post_at": last_post_at,
            "events_since_last_post": int(events_since),
            "hours_since_last_post": hours_since,
            "ever_posted": last_post_at is not None,
        }

    def feed_nudge(self, *, project_id: str, conn: Any) -> dict[str, Any] | None:
        """A soft 'consider posting' hint, or None when nothing needs saying.

        Backup only: fires when a main agent has been silent for an extended
        stretch (both event-count AND elapsed-time thresholds crossed). Never
        blocks — the feed is ungated by design.
        """
        signal = self.feed_signal(project_id=project_id, conn=conn)
        events = signal["events_since_last_post"]
        hours = signal["hours_since_last_post"]
        if events < feed_policy.NUDGE_AFTER_EVENTS:
            return None
        if hours is not None and hours < feed_policy.NUDGE_AFTER_HOURS:
            return None
        if signal["ever_posted"]:
            reason = (
                f"{events} things have happened and roughly "
                f"{int(hours)}h have passed since your last feed post"
                if hours is not None
                else f"{events} things have happened since your last feed post"
            )
        else:
            reason = (
                f"{events} things have happened and there are no feed posts yet"
            )
        return {
            "should_post": True,
            "hint": (
                f"Consider posting to the feed — {reason}. Share one high-signal "
                "aha-moment if there is something worth surfacing (brief; a visual "
                "helps). Skip it if nothing rises to that bar."
            ),
            **signal,
        }

    # -- event-carried advisory (rides tool responses, not feed.list) -------

    def feed_note_for(
        self, *, project_id: str, entity_id: str, event: str
    ) -> str | None:
        """A one-line, optional advisory for another tool's response.

        The nudge above only reaches an agent that already remembers to call
        feed.list. This is the other half: other services attach this note to
        their own tool responses at story-worthy moments (an experiment
        completing, a review verdict landing, an MLflow run finishing), so an
        agent that never thinks to check the feed still gets reminded it
        exists — reusing a response it was already going to read.

        Returns None once the feed has said anything at all about
        ``entity_id`` in this project — either a post's ``ref`` points at it,
        or its ``text`` mentions it inline — so there is no separate "already
        nudged" state to track: the dedup is just "has anyone posted about
        this yet".

        Never raises on missing/blank inputs (returns None); callers still
        wrap the call, since a feed hiccup must never break the workflow
        transition whose response this rides on.
        """
        project_id = (project_id or "").strip()
        entity_id = (entity_id or "").strip()
        if not project_id or not entity_id:
            return None
        conn = self.store.connect()
        try:
            mentioned = conn.execute(
                "SELECT 1 FROM posts WHERE project_id = ? "
                "AND (ref = ? OR text LIKE ? ESCAPE '\\') LIMIT 1",
                (project_id, entity_id, f"%{_escape_like(entity_id)}%"),
            ).fetchone()
        finally:
            conn.close()
        if mentioned is not None:
            return None
        phrase = _FEED_NOTE_PHRASES.get(
            event, "{entity} just had a workflow update"
        ).format(entity=entity_id)
        return (
            f"{phrase} and the feed has never mentioned it — if there's a "
            "takeaway worth sharing, consider a post (see the feed-posting skill)."
        )


# Phrasing for feed_note_for, keyed by the caller-supplied ``event`` — kept as
# plain data so new attach points can add a phrase without touching the
# lookup logic. Unknown events fall back to a generic phrase (see above).
_FEED_NOTE_PHRASES: dict[str, str] = {
    "experiment_complete": "{entity} just completed",
    "experiment_failed": "{entity} just failed",
    "experiment_abandoned": "{entity} was just abandoned",
    "experiment_review_verdict": "a review verdict just landed on {entity}",
    "mlflow_run_finalized": "an MLflow run for {entity} just finished",
}


def _escape_like(value: str) -> str:
    """Escape SQL LIKE metacharacters so ``value`` is matched literally.

    Entity ids commonly contain ``_`` (``exp_``, `claim_``, ...), itself a
    LIKE single-char wildcard — left unescaped it would make the substring
    search too permissive. ``LIKE ... ESCAPE '\\'`` is portable across both
    the SQLite and Postgres dialects (unlike SQLite-only ``instr``).
    """
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _hours_since(iso_ts: str | None) -> float | None:
    parsed = parse_iso(iso_ts)
    if parsed is None:
        return None
    from datetime import UTC, datetime

    delta = datetime.now(UTC) - parsed
    return max(0.0, delta.total_seconds() / 3600.0)
