"""Local feed image reads for feed.post."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.feed_images import MAX_FEED_IMAGE_BYTES
from ..utils import ValidationError
from .repo_paths import resolve_repo_path


class LocalFeedImageReader:
    """Read one repo image file without writing feed records."""

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def read_image(self, *, path: str) -> dict[str, Any]:
        _rel_path, file_path = resolve_repo_path(
            repo_root=self.repo_root, path=path, subject="image path"
        )
        if not file_path.is_file():
            raise ValidationError(f"image not found: {path}")
        size = file_path.stat().st_size
        if size > MAX_FEED_IMAGE_BYTES:
            raise ValidationError(
                f"image is {size} bytes; keep feed images under {MAX_FEED_IMAGE_BYTES}"
            )
        return {
            # Keep cloud-bound metadata path-shaped only enough for image sniffing;
            # never send repo_root or an absolute local filename upstream.
            "path": file_path.name or "feed-image",
            "data": file_path.read_bytes(),
        }
