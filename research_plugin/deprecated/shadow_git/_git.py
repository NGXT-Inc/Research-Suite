"""Thin wrapper around the `git` CLI for the shadow store."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import ShadowGitCommitError, ShadowGitUnavailableError


class GitCli:
    def __init__(self, *, repo: Path) -> None:
        self.repo = repo

    def run(self, args: tuple[str, ...], *, allow_nonzero: bool = False) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ShadowGitUnavailableError(
                "git binary not found on PATH; shadow git cannot operate"
            ) from exc

    def call(self, args: tuple[str, ...]) -> str:
        result = self.run(args)
        if result.returncode != 0:
            raise ShadowGitCommitError(
                f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout.strip()

    def has_head(self) -> bool:
        return self.run(("rev-parse", "--verify", "HEAD")).returncode == 0

    def ensure_initialised(self) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        if (self.repo / ".git").exists():
            return
        self.call(("init", "--quiet"))
        self.call(("config", "user.name", "Research Plugin"))
        self.call(("config", "user.email", "research-plugin@local"))
        self.call(("config", "commit.gpgsign", "false"))

    def restore_clean_worktree(self) -> None:
        # If we have a HEAD, reset to it. Either way, drop staged paths and
        # untracked files so the next snapshot starts from a known state.
        if self.has_head():
            self.call(("reset", "--hard", "--quiet", "HEAD"))
        else:
            self.run(("rm", "-rf", "--cached", "--ignore-unmatch", "."))
        self.call(("clean", "-fdq", "--", "."))

    def stage(self, path: str) -> None:
        self.call(("add", "--", path))

    def staged_changes(self) -> bool:
        result = self.run(("diff", "--cached", "--quiet"))
        if result.returncode not in (0, 1):
            raise ShadowGitCommitError(f"git diff --cached failed: {result.stderr.strip()}")
        return result.returncode == 1

    def commit(self, *, subject: str, body: str) -> str:
        self.call(
            (
                "commit",
                "--no-gpg-sign",
                "--quiet",
                "-m",
                subject,
                "-m",
                body,
            )
        )
        return self.call(("rev-parse", "HEAD"))

    def show(self, *, commit: str, path: str) -> bytes:
        result = self.run(("show", f"{commit}:{path}"))
        if result.returncode != 0:
            raise ShadowGitCommitError(result.stderr.strip())
        # subprocess with text=True returns str; re-encode for callers that decode themselves.
        return result.stdout.encode("utf-8")

    def diff(self, *, from_commit: str, to_commit: str, path: str) -> str:
        return self.call(("diff", from_commit, to_commit, "--", path))

    def remove_path(self, path: str) -> None:
        self.run(("rm", "-r", "--ignore-unmatch", "--", path))


def remove_filesystem_conflict(absolute_path: Path) -> None:
    if not absolute_path.exists():
        return
    if absolute_path.is_dir():
        shutil.rmtree(absolute_path)
    else:
        absolute_path.unlink()
