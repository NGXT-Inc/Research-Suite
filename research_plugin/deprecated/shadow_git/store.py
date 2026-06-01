"""Shadow git store: snapshot small text resources into a private git repo."""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:  # pragma: no cover - Unix only; tested on darwin/linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from . import _policy as policy
from ._git import GitCli, remove_filesystem_conflict
from .errors import (
    ShadowGitCommitError,
    ShadowGitError,
    ShadowGitPathError,
    SnapshotUnavailableError,
)


class ShadowGitStore:
    """Stores small text resource snapshots in a private git repository.

    Consultation is gated by ``RESEARCH_PLUGIN_SHADOW_GIT_ENABLED`` so the rest
    of the app can fall back to metadata-only snapshots when shadow git is
    temporarily disabled. The disabled path never touches git.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        max_snapshot_bytes: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.state_dir = self.repo_root / ".research_plugin"
        self.git_root = self.state_dir / "resource_store.git"
        self.lock_path = self.state_dir / "resource_store.lock"
        self.max_snapshot_bytes = policy.resolve_max_snapshot_bytes(value=max_snapshot_bytes)
        self.enabled = policy.is_enabled() if enabled is None else enabled
        self._git = GitCli(repo=self.git_root)

    # ---------- public API ----------

    def snapshot_file(
        self,
        *,
        project_id: str,
        rel_path: str,
        file_path: Path,
        observed_at: str,
        created_by: str,
    ) -> dict[str, Any]:
        size_bytes = file_path.stat().st_size
        binary = policy.is_binary(file_path)
        sha = (
            policy.metadata_sha256(file_path, size_bytes)
            if size_bytes > self.max_snapshot_bytes
            else policy.content_sha256(file_path)
        )
        ctype = policy.content_type(rel_path, file_path, binary=binary)
        if not self.enabled or size_bytes > self.max_snapshot_bytes or binary:
            return self._metadata_only(sha=sha, ctype=ctype)

        git_path = policy.safe_git_path(project_id=project_id, rel_path=rel_path)
        with self._locked():
            self._git.ensure_initialised()
            try:
                self._git.restore_clean_worktree()
                commit = self._store_text_snapshot(
                    git_path=git_path,
                    rel_path=rel_path,
                    file_path=file_path,
                    project_id=project_id,
                    observed_at=observed_at,
                    created_by=created_by,
                )
            except (ShadowGitError, OSError):
                # Leave the worktree clean for the next caller; never crash
                # downstream because of a transient shadow-git failure.
                self._safe_restore()
                raise

        return {
            "git_path": git_path,
            "git_commit": commit,
            "content_sha256": sha,
            "content_type": ctype,
            "snapshot_status": "stored",
        }

    def version_text(self, *, git_commit: str, git_path: str) -> str:
        if not self.enabled:
            raise SnapshotUnavailableError("shadow git is disabled")
        policy.validate_commit_hash(git_commit)
        with self._locked():
            self._require_repo()
            data = self._git.show(commit=git_commit, path=git_path)
        return data.decode("utf-8", errors="replace")

    def diff(self, *, from_commit: str, to_commit: str, git_path: str) -> str:
        if not self.enabled:
            raise SnapshotUnavailableError("shadow git is disabled")
        policy.validate_commit_hash(from_commit)
        policy.validate_commit_hash(to_commit)
        with self._locked():
            self._require_repo()
            try:
                return self._git.diff(from_commit=from_commit, to_commit=to_commit, path=git_path)
            except ShadowGitCommitError as exc:
                raise SnapshotUnavailableError(str(exc)) from exc

    # Legacy helpers kept for callers that previously imported them.
    def content_sha256(self, *, file_path: Path) -> str:
        return policy.content_sha256(file_path)

    def metadata_sha256(self, *, file_path: Path, size_bytes: int) -> str:
        return policy.metadata_sha256(file_path, size_bytes)

    def content_type(self, *, rel_path: str, file_path: Path, is_binary: bool | None = None) -> str:
        binary = policy.is_binary(file_path) if is_binary is None else is_binary
        return policy.content_type(rel_path, file_path, binary=binary)

    # ---------- internals ----------

    def _metadata_only(self, *, sha: str, ctype: str) -> dict[str, Any]:
        return {
            "git_path": None,
            "git_commit": None,
            "content_sha256": sha,
            "content_type": ctype,
            "snapshot_status": "metadata_only",
        }

    def _store_text_snapshot(
        self,
        *,
        git_path: str,
        rel_path: str,
        file_path: Path,
        project_id: str,
        observed_at: str,
        created_by: str,
    ) -> str:
        destination = self.git_root / git_path
        self._prepare_destination(destination=destination, git_path=git_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file_path, destination)
        self._git.stage(git_path)
        if self._git.staged_changes():
            return self._git.commit(
                subject=f"Snapshot {rel_path}",
                body=(
                    f"project={project_id}\npath={rel_path}\n"
                    f"observed_at={observed_at}\ncreated_by={created_by}"
                ),
            )
        return self._git.call(("rev-parse", "HEAD"))

    def _prepare_destination(self, *, destination: Path, git_path: str) -> None:
        relative_parts = Path(git_path).parts
        current = self.git_root
        for index, part in enumerate(relative_parts[:-1], start=1):
            current = current / part
            if current.exists() and not current.is_dir():
                conflict_path = Path(*relative_parts[:index]).as_posix()
                self._git.remove_path(conflict_path)
                remove_filesystem_conflict(current)
                return
        if destination.exists() and destination.is_dir():
            self._git.remove_path(git_path)
            remove_filesystem_conflict(destination)

    def _safe_restore(self) -> None:
        try:
            self._git.restore_clean_worktree()
        except ShadowGitError:
            pass

    def _require_repo(self) -> None:
        if not (self.git_root / ".git").is_dir():
            raise SnapshotUnavailableError("shadow git repository is not initialised")

    @contextmanager
    def _locked(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if fcntl is None:  # pragma: no cover
            yield
            return
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # Legacy path-builder kept for callers that still import it directly.
    def _git_path(self, *, project_id: str, rel_path: str) -> str:
        try:
            return policy.safe_git_path(project_id=project_id, rel_path=rel_path)
        except ShadowGitPathError as exc:
            raise ShadowGitPathError(str(exc)) from exc
