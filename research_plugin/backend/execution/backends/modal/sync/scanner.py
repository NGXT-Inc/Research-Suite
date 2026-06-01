"""Local and remote scanners. Produce {path: FileFingerprint} dictionaries."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from typing import Any

from .types import FileFingerprint


# Directories whose entire contents we never sync.
HARDCODED_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".research_plugin",
        ".research_plugin_job",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        ".aws",
        "node_modules",
        ".DS_Store",
    }
)

HARDCODED_EXCLUDED_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo")

# Path prefixes (repo-relative, posix) that are pruned from sync entirely.
# These hold large, volume-managed, reproducible-on-demand data that the job
# writes/reads directly on the Modal Volume and must never round-trip to the
# local repo — otherwise the bidirectional submit-time sync tries to pull tens
# of GB back to the daemon host and blows past the submit watchdog. Experiment
# outputs (declared expected_outputs) live under experiments/ and still sync.
HARDCODED_EXCLUDED_PREFIXES: tuple[str, ...] = ("data/raw", "data/processed")


def local_scan(*, repo_root: Path) -> dict[str, FileFingerprint]:
    """Walk the local repo, stat every file, return {rel_path: fingerprint}.

    Symlinks are skipped. Excluded directory names are pruned.
    """
    repo_root = repo_root.resolve()
    result: dict[str, FileFingerprint] = {}
    for path in _iter_local_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        if _excluded(rel):
            continue
        stat = path.stat()
        result[rel] = FileFingerprint(
            path=rel,
            mtime_ns=int(stat.st_mtime_ns),
            size_bytes=int(stat.st_size),
        )
    return result


def remote_scan(*, volume: Any, repo_dir: str = "") -> dict[str, FileFingerprint]:
    """List the modal volume recursively under repo_dir, return {rel_path: fingerprint}.

    The volume content is treated as a direct mirror of the local repo root, so
    paths returned are repo-relative (no repo_dir prefix). If repo_dir is empty
    the entire volume is treated as the repo.
    """
    prefix = repo_dir.strip("/")
    listdir = getattr(volume, "listdir", None)
    if listdir is None:
        raise RuntimeError("modal volume object has no listdir; cannot scan remote")

    base = prefix or "/"
    raw_entries = listdir(base, recursive=True)
    entries = _collect_iter(raw_entries)

    result: dict[str, FileFingerprint] = {}
    for entry in entries:
        if not _entry_is_file(entry):
            continue
        full_path = str(getattr(entry, "path", "")).lstrip("/")
        if not full_path:
            continue
        rel = (
            full_path[len(prefix) + 1 :]
            if prefix and full_path.startswith(f"{prefix}/")
            else full_path
        )
        if not rel or _excluded(rel):
            continue
        mtime = getattr(entry, "mtime", 0) or 0
        size = int(getattr(entry, "size", 0) or 0)
        result[rel] = FileFingerprint(
            path=rel,
            mtime_ns=int(float(mtime) * 1_000_000_000),
            size_bytes=size,
        )
    return result


def _iter_local_files(repo_root: Path):
    """rglob with directory pruning. Yields file Path objects only."""
    stack: list[Path] = [repo_root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            name = entry.name
            if name in HARDCODED_EXCLUDED_NAMES:
                continue
            if entry.is_dir():
                stack.append(entry)
                continue
            if not entry.is_file():
                continue
            if name.endswith(HARDCODED_EXCLUDED_SUFFIXES):
                continue
            yield entry


def _excluded(rel_path: str) -> bool:
    parts = PurePosixPath(rel_path).parts
    if any(part in HARDCODED_EXCLUDED_NAMES for part in parts):
        return True
    for prefix in HARDCODED_EXCLUDED_PREFIXES:
        if rel_path == prefix or rel_path.startswith(prefix + "/"):
            return True
    return rel_path.endswith(HARDCODED_EXCLUDED_SUFFIXES)


def _entry_is_file(entry: Any) -> bool:
    kind = getattr(entry, "type", None)
    if kind is None:
        return True
    value = getattr(kind, "value", kind)
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"file", "regular"}:
            return True
        if lowered in {"directory", "dir", "symlink", "link"}:
            return False
    # Enums in modal: FileEntryType.FILE == 1, DIRECTORY == 2, SYMLINK == 3
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return True


def _collect_iter(value: Any) -> list[Any]:
    if hasattr(value, "__aiter__"):
        async def _gather() -> list[Any]:
            return [item async for item in value]

        return asyncio.run(_gather())
    return list(value)
