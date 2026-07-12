"""Local feed HTML embed reads for feed.post."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.feed_embeds import MAX_FEED_EMBED_BYTES, sniff_html_type
from ..utils import ValidationError
from .repo_paths import resolve_repo_path


class LocalFeedEmbedReader:
    """Read one repo HTML file without writing feed records."""

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def read_embed(self, *, path: str) -> dict[str, Any]:
        _rel_path, file_path = resolve_repo_path(
            repo_root=self.repo_root, path=path, subject="embed path"
        )
        if not file_path.is_file():
            raise ValidationError(f"embed not found: {path}")
        size = file_path.stat().st_size
        if size > MAX_FEED_EMBED_BYTES:
            raise ValidationError(
                f"embed is {size} bytes; keep feed embeds under {MAX_FEED_EMBED_BYTES}"
            )
        data = file_path.read_bytes()
        if sniff_html_type(data) is None:
            raise ValidationError(f"{path} does not look like an HTML document")
        return {
            "path": file_path.name or "feed-embed",
            "data": data,
        }
