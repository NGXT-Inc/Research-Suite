"""Apply a SyncPlan: push to volume, pull to local, delete on either side.

After apply we re-fingerprint the touched paths so the baseline reflects the
post-apply state of both sides accurately.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .scanner import remote_scan
from .types import FileFingerprint, SyncPlan


@dataclass(frozen=True)
class ApplyOutcome:
    pushed: int
    pulled: int
    deleted_remote: int
    deleted_local: int
    # Post-apply fingerprints for paths the apply touched, keyed by repo-relative path.
    # Used by the engine to write the new baseline.
    fingerprints: dict[str, tuple[FileFingerprint | None, FileFingerprint | None]]


class SyncApplier:
    """Applies a SyncPlan against a writable Modal volume + local repo."""

    def __init__(self, *, repo_root: Path, repo_dir: str = "") -> None:
        self.repo_root = repo_root.resolve()
        self.repo_dir = repo_dir.strip("/")

    def apply(self, *, volume: Any, plan: SyncPlan) -> ApplyOutcome:
        pushed = self._push(volume=volume, fingerprints=plan.push)
        deleted_remote = self._delete_remote(volume=volume, paths=plan.delete_remote)
        pulled = self._pull(volume=volume, fingerprints=plan.pull)
        deleted_local = self._delete_local(paths=plan.delete_local)

        touched_paths = (
            tuple(fp.path for fp in plan.push)
            + tuple(fp.path for fp in plan.pull)
            + tuple(plan.delete_remote)
            + tuple(plan.delete_local)
            + tuple(fp.path for fp in plan.converged)
        )
        fingerprints = self._refingerprint(volume=volume, paths=touched_paths)
        return ApplyOutcome(
            pushed=pushed,
            pulled=pulled,
            deleted_remote=deleted_remote,
            deleted_local=deleted_local,
            fingerprints=fingerprints,
        )

    # ---------- push ----------

    def _push(
        self, *, volume: Any, fingerprints: tuple[FileFingerprint, ...]
    ) -> int:
        if not fingerprints:
            return 0
        batch_upload = getattr(volume, "batch_upload", None)
        if batch_upload is None:
            raise RuntimeError("modal volume has no batch_upload method")
        with batch_upload(force=True) as batch:
            for fp in fingerprints:
                local = self.repo_root / fp.path
                remote = self._volume_path(fp.path)
                batch.put_file(str(local), remote)
        return len(fingerprints)

    def _delete_remote(self, *, volume: Any, paths: tuple[str, ...]) -> int:
        if not paths:
            return 0
        remove = getattr(volume, "remove_file", None)
        if remove is None:
            raise RuntimeError("modal volume has no remove_file method")
        count = 0
        for path in paths:
            try:
                _await_if_needed(remove(self._volume_path(path), recursive=False))
                count += 1
            except FileNotFoundError:
                pass
            except Exception:
                # Non-fatal; baseline will keep this path as still-present remotely
                # and the next pass will retry.
                continue
        return count

    # ---------- pull ----------

    def _pull(
        self, *, volume: Any, fingerprints: tuple[FileFingerprint, ...]
    ) -> int:
        if not fingerprints:
            return 0
        read_file = getattr(volume, "read_file", None)
        if read_file is None:
            raise RuntimeError("modal volume has no read_file method")
        count = 0
        for fp in fingerprints:
            local = self.repo_root / fp.path
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(f".{local.name}.modal-sync-tmp")
            data = self._read_volume_bytes(volume=volume, path=self._volume_path(fp.path))
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, local)
            count += 1
        return count

    def _delete_local(self, *, paths: tuple[str, ...]) -> int:
        count = 0
        for path in paths:
            local = self.repo_root / path
            try:
                if local.is_file():
                    local.unlink()
                    count += 1
            except FileNotFoundError:
                pass
            except OSError:
                continue
        return count

    # ---------- post-apply rescan ----------

    def _refingerprint(
        self, *, volume: Any, paths: tuple[str, ...]
    ) -> dict[str, tuple[FileFingerprint | None, FileFingerprint | None]]:
        """Get fresh local + remote fingerprints for the touched paths."""
        if not paths:
            return {}
        unique_paths = set(paths)
        remote_index = remote_scan(volume=volume, repo_dir=self.repo_dir)
        result: dict[str, tuple[FileFingerprint | None, FileFingerprint | None]] = {}
        for path in unique_paths:
            local_path = self.repo_root / path
            local_fp: FileFingerprint | None = None
            if local_path.is_file():
                stat = local_path.stat()
                local_fp = FileFingerprint(
                    path=path,
                    mtime_ns=int(stat.st_mtime_ns),
                    size_bytes=int(stat.st_size),
                )
            remote_fp = remote_index.get(path)
            result[path] = (local_fp, remote_fp)
        return result

    # ---------- helpers ----------

    def _volume_path(self, rel_path: str) -> str:
        if self.repo_dir:
            return PurePosixPath(self.repo_dir, rel_path).as_posix()
        return rel_path

    def _read_volume_bytes(self, *, volume: Any, path: str) -> bytes:
        chunks = volume.read_file(path)
        if isinstance(chunks, (bytes, bytearray)):
            return bytes(chunks)
        if hasattr(chunks, "__aiter__"):
            async def _collect() -> bytes:
                return b"".join([_as_bytes(chunk) async for chunk in chunks])

            return asyncio.run(_collect())
        return b"".join(_as_bytes(chunk) for chunk in chunks)


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8")


def _await_if_needed(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return asyncio.run(_run_awaitable(value))
    return value


async def _run_awaitable(value: Any) -> Any:
    return await value
