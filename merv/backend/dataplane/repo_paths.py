"""Repo path guards for local data-plane file reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_plugin_shared.project_dirs import PROJECT_STATE_DIR_NAMES

from ..utils import ValidationError


def repo_relative_path(*, path: str, subject: str = "path") -> str:
    if not path:
        raise ValidationError(f"{subject} is required")
    rel = Path(path)
    if rel.is_absolute():
        raise ValidationError(f"{subject} must be repo-relative")
    if any(part == ".." for part in rel.parts):
        raise ValidationError(f"{subject} may not contain '..'")
    if rel.parts and rel.parts[0] in PROJECT_STATE_DIR_NAMES:
        raise ValidationError(
            f"{subject} may not point inside the project state dir "
            "(.merv or .research_plugin)"
        )
    return rel.as_posix()


def resolve_repo_path(
    *, repo_root: Any, path: str, subject: str = "path"
) -> tuple[str, Path]:
    rel_path = repo_relative_path(path=path, subject=subject)
    root = Path(repo_root).resolve()
    full = (root / rel_path).resolve()
    try:
        full.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{subject} escapes repo root") from exc
    return rel_path, full
