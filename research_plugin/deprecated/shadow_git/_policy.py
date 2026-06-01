"""Pure helpers for shadow git: env flag, sizing, hashing, path safety."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from pathlib import Path

from .errors import ShadowGitConfigError, ShadowGitPathError


DEFAULT_MAX_SNAPSHOT_BYTES = 5_000_000
METADATA_SAMPLE_BYTES = 1024 * 1024
_HASH_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")


def is_enabled() -> bool:
    raw = os.environ.get("RESEARCH_PLUGIN_SHADOW_GIT_ENABLED", "0")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_max_snapshot_bytes(value: int | None) -> int:
    if value is None:
        configured = os.environ.get("RESEARCH_PLUGIN_MAX_SNAPSHOT_BYTES")
        if configured in {None, ""}:
            value = DEFAULT_MAX_SNAPSHOT_BYTES
        else:
            try:
                value = int(configured)
            except ValueError as exc:
                raise ShadowGitConfigError(
                    "RESEARCH_PLUGIN_MAX_SNAPSHOT_BYTES must be an integer"
                ) from exc
    if value < 0:
        raise ShadowGitConfigError("max snapshot bytes must be non-negative")
    return value


def is_binary(file_path: Path) -> bool:
    with file_path.open("rb") as handle:
        sample = handle.read(8192)
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def content_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metadata_sha256(file_path: Path, size_bytes: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"metadata-only:{size_bytes}:".encode("utf-8"))
    with file_path.open("rb") as handle:
        digest.update(handle.read(METADATA_SAMPLE_BYTES))
        if size_bytes > METADATA_SAMPLE_BYTES:
            handle.seek(max(size_bytes - METADATA_SAMPLE_BYTES, 0))
            digest.update(handle.read(METADATA_SAMPLE_BYTES))
    return digest.hexdigest()


def content_type(rel_path: str, file_path: Path, binary: bool) -> str:
    guessed = mimetypes.guess_type(rel_path)[0]
    if guessed:
        return guessed
    return "application/octet-stream" if binary else "text/plain"


def safe_git_path(project_id: str, rel_path: str) -> str:
    if not project_id or not rel_path:
        raise ShadowGitPathError("project_id and rel_path are required")
    path = Path(rel_path)
    if path.is_absolute():
        raise ShadowGitPathError("rel_path must be repo-relative")
    parts = path.parts
    if any(part == ".." for part in parts):
        raise ShadowGitPathError("rel_path may not contain '..'")
    if any(part == ".git" for part in parts):
        raise ShadowGitPathError("rel_path may not contain a '.git' segment")
    if any(part == "" for part in parts):
        raise ShadowGitPathError("rel_path may not contain empty segments")
    return f"projects/{project_id}/{path.as_posix()}"


def validate_commit_hash(value: str) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.match(value):
        raise ShadowGitPathError(f"invalid git commit hash: {value!r}")
    return value
