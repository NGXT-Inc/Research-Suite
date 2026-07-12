"""Shared feed image limits and content sniffing."""

from __future__ import annotations

import mimetypes
from pathlib import Path


# Raster types we will re-host from ANY source, including external (unfurl)
# og:images. Deliberately SVG-free: an external SVG served same-origin is stored
# XSS, so it never enters this set.
SERVEABLE_IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
SVG_IMAGE_TYPE = "image/svg+xml"
# Types an agent may attach by local path (feed.post image_path). SVG is allowed
# here — it is first-party (the agent's own chart) and served INERT via a CSP
# sandbox header (see transport/feed_http.py), so it cannot script. Kept separate
# from SERVEABLE_IMAGE_TYPES on purpose so the unfurl path stays raster-only.
UPLOADABLE_IMAGE_TYPES = SERVEABLE_IMAGE_TYPES | {SVG_IMAGE_TYPE}
MAX_FEED_IMAGE_BYTES = 10_000_000

# SVG is text, not magic-byte-framed; scan only this many leading bytes for its
# root element so a huge file can't turn the sniff into a full read.
_SVG_SNIFF_PREFIX = 4096


def _looks_like_svg(data: bytes) -> bool:
    """True when the bytes open an SVG document root (after BOM/XML decl/comment)."""
    head = data[:_SVG_SNIFF_PREFIX].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if not head.startswith((b"<?xml", b"<!doctype svg", b"<svg", b"<!--")):
        return False
    return b"<svg" in head


def sniff_image_type(path: Path, data: bytes) -> str | None:
    """Best-effort image content-type from magic bytes, then SVG root, then extension."""
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if _looks_like_svg(data):
        return SVG_IMAGE_TYPE
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed in UPLOADABLE_IMAGE_TYPES:
        return guessed
    return None
