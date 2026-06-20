"""Local gated-artifact reads for resource.associate."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from ..domain.markdown_images import (
    MARKDOWN_FIGURE_MAX_BYTES,
    markdown_image_links,
    markdown_image_targets,
)
from ..domain.vocabulary import GATED_ROLE_BYTE_CAPS
from ..utils import NotFoundError, ValidationError
from .repo_paths import resolve_repo_path


MARKDOWN_FIGURE_ROLES = frozenset({"report", "reflection_doc", "synthesis_doc"})


class LocalResourceArtifactReader:
    """Read gated resource artifact bytes without mutating control records."""

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def read_for_association(self, *, path: str, role: str) -> dict[str, Any]:
        cap = GATED_ROLE_BYTE_CAPS.get(role)
        if cap is None:
            return {"content_bytes": None, "figures": []}
        rel_path, file_path = self._resolve_file(path=path)
        size = file_path.stat().st_size
        if size > cap:
            raise ValidationError(
                f"{rel_path} is {size} bytes; the maximum for a role-{role!r} "
                f"artifact is {cap} bytes — slim the file before associating "
                "(move raw data/outputs elsewhere and reference them)",
                details={
                    "path": rel_path,
                    "role": role,
                    "size_bytes": size,
                    "max_bytes": cap,
                },
            )
        data = file_path.read_bytes()
        figures: list[dict[str, Any]] = []
        if role in MARKDOWN_FIGURE_ROLES:
            markdown_text = data.decode("utf-8", errors="replace")
            reject_absolute_markdown_image_targets(
                markdown_rel_path=rel_path, markdown_text=markdown_text
            )
            figures = self.submitted_figures(
                markdown_rel_path=rel_path,
                markdown_text=markdown_text,
            )
        return {
            "content_bytes": data,
            "content_type": mimetypes.guess_type(rel_path)[0]
            or "application/octet-stream",
            "figures": figures,
        }

    def _resolve_file(self, *, path: str) -> tuple[str, Path]:
        rel_path, file_path = resolve_repo_path(
            repo_root=self.repo_root, path=path, subject="resource path"
        )
        if not file_path.exists():
            raise NotFoundError(f"resource file does not exist: {path}")
        if not file_path.is_file():
            raise ValidationError("v0.0001 resources must be files")
        return rel_path, file_path

    def submitted_figures(
        self, *, markdown_rel_path: str, markdown_text: str
    ) -> list[dict[str, Any]]:
        markdown_dir = (self.repo_root / markdown_rel_path).parent
        figures: list[dict[str, Any]] = []
        for link in markdown_image_links(markdown_text):
            resolved = (markdown_dir / link).resolve()
            try:
                resolved.relative_to(self.repo_root)
            except ValueError as exc:
                raise ValidationError(
                    f"report image link escapes the repo: {link}",
                    details={"link": link, "resource": markdown_rel_path},
                ) from exc
            if not resolved.is_file():
                continue
            size = resolved.stat().st_size
            if size > MARKDOWN_FIGURE_MAX_BYTES:
                continue
            data = resolved.read_bytes()
            figures.append(
                {
                    "link_path": link,
                    "data": data,
                    "content_type": mimetypes.guess_type(link)[0]
                    or "application/octet-stream",
                    "size_bytes": size,
                }
            )
        return figures


def reject_absolute_markdown_image_targets(
    *, markdown_rel_path: str, markdown_text: str
) -> None:
    for target in markdown_image_targets(markdown_text):
        if target.startswith("/") or os.path.isabs(target):
            raise ValidationError(
                f"markdown image link must be repo-relative: {target}",
                details={"link": target, "resource": markdown_rel_path},
            )
