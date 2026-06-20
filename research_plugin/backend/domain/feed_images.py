"""Shared feed image limits and content sniffing."""

from __future__ import annotations

import mimetypes
from pathlib import Path


SERVEABLE_IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
MAX_FEED_IMAGE_BYTES = 10_000_000


def sniff_image_type(path: Path, data: bytes) -> str | None:
    """Best-effort image content-type from magic bytes, then extension."""
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed in SERVEABLE_IMAGE_TYPES:
        return guessed
    return None
