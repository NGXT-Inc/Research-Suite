"""Local file observation for repo resources.

The control plane records resource facts; the data plane resolves repo paths
and hashes bytes. This module is the local implementation used by local mode
and the daemon before submitting an observation to control.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from ..kernel.ports.resource_records import ResourceObservation
from ..kernel.utils import NotFoundError, ValidationError
from .repo_paths import resolve_repo_path


class LocalResourceObserver:
    """Observe one repo file without writing resource records."""

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def observe_file(
        self,
        *,
        path: str,
        kind: str = "other",
        title: str = "",
        created_by: str = "codex",
    ) -> ResourceObservation:
        rel_path, file_path = self.resolve_repo_file(path=path)
        stat = file_path.stat()
        return {
            "path": rel_path,
            "kind": kind,
            "title": title,
            "created_by": created_by,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "size_bytes": stat.st_size,
            "content_sha256": content_sha256(file_path),
            "content_type": mimetypes.guess_type(rel_path)[0]
            or "application/octet-stream",
        }

    def resolve_repo_file(self, *, path: str) -> tuple[str, Path]:
        rel_path, full = resolve_repo_path(
            repo_root=self.repo_root, path=path, subject="resource path"
        )
        rel = Path(rel_path)
        if not full.exists():
            raise NotFoundError(f"resource file does not exist: {path}")
        if not full.is_file():
            raise ValidationError("v0.0001 resources must be files")
        return rel.as_posix(), full


def content_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
