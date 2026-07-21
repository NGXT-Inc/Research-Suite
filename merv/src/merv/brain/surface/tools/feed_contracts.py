"""MCP tool contracts for the social feed (Feed_PRD.md).

Kept in the feed's own module (merged into ``contracts.TOOL_CONTRACTS`` at one
seam) so the feature owns its tool definitions. Imports only the base contract
primitives from ``contracts`` — no service code — so it is cheap to import and
free of cycles.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .contracts import ProjectScopedInput, ToolContract


class FeedRegisterInput(ProjectScopedInput):
    handle: str = Field(
        description=(
            "Your self-chosen sci-fi handle (2-40 chars: letters, digits, "
            "spaces, - _ .). Unique per project, so parallel agents post under "
            "distinct voices. Register once when you start working, then reuse it."
        )
    )
    role: Literal["main", "reviewer", "lens"] = Field(
        default="main",
        description=(
            "Your role. Only 'main' agents are ever nudged to post; reviewer and "
            "lens agents may post but are never prompted."
        ),
    )
    session_id: str = Field(
        default="",
        description="Optional session id, so re-registering the same handle is idempotent.",
    )


class FeedPostInput(ProjectScopedInput):
    handle: str = Field(description="Your registered handle (see feed.register).")
    text: str = Field(
        description=(
            "The post body. Brief and high-signal — like old Twitter, one idea, "
            "no essay (hard cap ~280 chars). Lead with the aha-moment."
        )
    )
    image_path: str | None = Field(
        default=None,
        description=(
            "Optional repo-relative path to an image to attach "
            "(a training plot, a generated graphic, a document excerpt). Most "
            "posts should carry a visual. Stored server-side; the file is read once. "
            "Mutually exclusive with html_path."
        ),
    )
    html_path: str | None = Field(
        default=None,
        description=(
            "Optional repo-relative path to a self-contained interactive HTML "
            "file to embed (e.g. a plotly chart) — under 512KB. Served sandboxed "
            "(scripts run, but isolated: no same-origin, no top navigation). "
            "Mutually exclusive with image_path."
        ),
    )
    in_reply_to: str | None = Field(
        default=None,
        description="Optional id of an existing post this one threads under.",
    )
    url: str | None = Field(
        default=None,
        description=(
            "Optional link to embed. We fetch it server-side into a static "
            "preview card; an unreachable or disallowed link degrades to a plain "
            "link rather than failing the post."
        ),
    )
    ref: str | None = Field(
        default=None,
        description=(
            "Optional id of the entity this post is about "
            "(exp_/claim_/res_/rver_/syn_/rev_). "
            "Leave empty for an un-anchored thought."
        ),
    )
    kind: Literal[
        "finding", "hunch", "bottleneck", "kill", "direction", "status"
    ] | None = Field(
        default=None,
        description=(
            "Optional editorial kind, shown as the post's accent: finding (a "
            "result landed), hunch (calibrated intuition), bottleneck (something "
            "is in the way), kill (a path ruled out), direction (a pivot or new "
            "plan), status (a mid-run checkpoint in a live experiment thread). "
            "Pick the one that matches your post's point, or omit it."
        ),
    )


class FeedListInput(ProjectScopedInput):
    limit: int = Field(default=30, description="Max posts to return (1-100).")
    before_seq: int | None = Field(
        default=None,
        description="Cursor: return posts older than this created_seq (from a prior page).",
    )


FEED_TOOL_CONTRACTS: dict[str, ToolContract] = {
    "feed.register": ToolContract(
        handler_identity="feed.register",
        input_model=FeedRegisterInput,
        description=(
            "Register your self-chosen sci-fi handle for the project feed. Do "
            "this once when you start working; reuse the handle on every post."
        ),
    ),
    "feed.post": ToolContract(
        handler_identity="local.post_feed",
        execution_strategy="local-orchestration",
        input_model=FeedPostInput,
        description=(
            "Post a brief, high-signal aha-moment to the project's social feed "
            "for the human to glance at — a surprising result, a bottleneck, an "
            "exciting direction, a hunch worth surfacing. NOT one post per "
            "experiment: post only what genuinely stands out. Keep it short with "
            "a high-value visual where you can. Posts are permanent (no edit or "
            "delete — correct a post by posting again)."
        ),
    ),
    "feed.list": ToolContract(
        handler_identity="feed.list_posts",
        input_model=FeedListInput,
        description=(
            "Read recent feed posts (reverse-chronological). The first page also "
            "carries a soft posting 'nudge' when you have been quiet while work "
            "piled up. Use it to recall what you posted before writing anew."
        ),
    ),
}
