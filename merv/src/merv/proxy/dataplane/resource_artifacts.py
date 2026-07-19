"""Local gated-artifact reads for the resource.register association step."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from ..artifacts.markdown_images import (
    MARKDOWN_FIGURE_MAX_BYTES,
    MARKDOWN_FIGURE_ROLES,
    markdown_image_links,
    markdown_image_targets,
)
from ..artifacts.roles import GATED_ROLE_BYTE_CAPS, metric_result_capture_cap
from ..kernel.utils import NotFoundError, ValidationError
from .repo_paths import resolve_repo_path


class LocalResourceArtifactReader:
    """Read gated resource artifact bytes without mutating control records."""

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def read_for_association(self, *, path: str, role: str) -> dict[str, Any]:
        cap = GATED_ROLE_BYTE_CAPS.get(role)
        if cap is None:
            return self._read_metric_result(path=path, role=role)
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

    def _read_metric_result(self, *, path: str, role: str) -> dict[str, Any]:
        """Bytes for a metric-file result association (metrics exhibit source).

        Opportunistic, unlike the gated read: an over-cap or unreadable file
        associates without pinned bytes instead of erroring — result files may
        legitimately be large, and the exhibit simply won't ingest them."""
        empty = {"content_bytes": None, "figures": []}
        cap = metric_result_capture_cap(role=role, path=path)
        if cap is None:
            return empty
        try:
            rel_path, file_path = self._resolve_file(path=path)
        except (NotFoundError, ValidationError):
            return empty
        if file_path.stat().st_size > cap:
            return empty
        return {
            "content_bytes": file_path.read_bytes(),
            "content_type": mimetypes.guess_type(rel_path)[0] or "application/json",
            "figures": [],
        }

    def read_for_backfill(self, *, path: str, role: str) -> dict[str, Any] | None:
        if role not in GATED_ROLE_BYTE_CAPS:
            return None
        try:
            rel_path, file_path = self._resolve_file(path=path)
            data = file_path.read_bytes()
        except (OSError, NotFoundError, ValidationError):
            return None
        figures: list[dict[str, Any]] = []
        if role in MARKDOWN_FIGURE_ROLES:
            try:
                figures = self.submitted_figures(
                    markdown_rel_path=rel_path,
                    markdown_text=data.decode("utf-8", errors="replace"),
                )
            except ValidationError:
                figures = []
        return {
            "content_bytes": data,
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
            problem = figure_link_problem(
                repo_root=self.repo_root,
                markdown_rel_path=markdown_rel_path,
                link=link,
            )
            if problem is not None:
                raise ValidationError(
                    f"{problem} — save the figure next to {markdown_rel_path} "
                    "(copy it off the sandbox first if it was produced there) "
                    "or drop the link, then associate again",
                    details={"link": link, "resource": markdown_rel_path},
                )
            resolved = (markdown_dir / link).resolve()
            data = resolved.read_bytes()
            figures.append(
                {
                    "link_path": link,
                    "data": data,
                    "content_type": mimetypes.guess_type(link)[0]
                    or "application/octet-stream",
                    "size_bytes": len(data),
                }
            )
        return figures


def figure_link_problem(
    *, repo_root: Path, markdown_rel_path: str, link: str
) -> str | None:
    """Per-link acceptance rule — exists, is a file, within the figure size
    cap — shared by the validate preflight and the associate-time capture so
    the preflight can never pass a link the capture would reject."""
    resolved = ((repo_root / markdown_rel_path).parent / link).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        return f"figure {link!r} escapes the repo"
    if not resolved.exists():
        return f"figure {link!r} has no submitted content: file does not exist"
    if not resolved.is_file():
        return f"figure {link!r} has no submitted content: target is not a file"
    size = resolved.stat().st_size
    if size > MARKDOWN_FIGURE_MAX_BYTES:
        return (
            f"figure {link!r} is {size} bytes; the maximum figure size is "
            f"{MARKDOWN_FIGURE_MAX_BYTES} bytes"
        )
    return None


def reject_absolute_markdown_image_targets(
    *, markdown_rel_path: str, markdown_text: str
) -> None:
    for target in markdown_image_targets(markdown_text):
        if target.startswith("/") or os.path.isabs(target):
            raise ValidationError(
                f"markdown image link must be repo-relative: {target}",
                details={"link": target, "resource": markdown_rel_path},
            )
