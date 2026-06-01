"""Helpers for locating a running research_plugin HTTP daemon.

Other processes in the same repo (notably the stdio MCP proxy) need to find
out where the daemon is listening. The daemon writes a small JSON marker at
``.research_plugin/daemon.json`` on startup and removes it on shutdown. The
marker is best-effort metadata: if it goes stale (process crashed without
cleanup), the proxy falls back to a clear "daemon not reachable" error rather
than silently using the stale URL.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import now_iso


MARKER_FILENAME = "daemon.json"


def marker_path(*, repo_root: Path) -> Path:
    return repo_root / ".research_plugin" / MARKER_FILENAME


@dataclass(frozen=True)
class DaemonInfo:
    host: str
    port: int
    pid: int
    started_at: str
    repo_root: str

    @property
    def url(self) -> str:
        host = self.host
        # Wrap IPv6 literals so urllib parses them correctly.
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "pid": self.pid,
            "started_at": self.started_at,
            "repo_root": self.repo_root,
        }


def write_marker(*, repo_root: Path, host: str, port: int, pid: int | None = None) -> Path:
    """Write the daemon marker. Best-effort: returns the path even if write fails."""
    info = DaemonInfo(
        host=host,
        port=int(port),
        pid=int(pid if pid is not None else os.getpid()),
        started_at=now_iso(),
        repo_root=str(repo_root),
    )
    path = marker_path(repo_root=repo_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(info.to_dict(), sort_keys=True), encoding="utf-8")
    except OSError:
        # Don't fail daemon startup over an unwritable marker. Discovery will
        # just fall through to RESEARCH_PLUGIN_DAEMON_URL or an actionable error.
        pass
    return path


def clear_marker(*, repo_root: Path) -> None:
    """Remove the daemon marker. Idempotent; ignores missing/permission errors."""
    path = marker_path(repo_root=repo_root)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_marker(*, repo_root: Path) -> DaemonInfo | None:
    """Read the daemon marker if present. Returns None on missing/corrupt files."""
    path = marker_path(repo_root=repo_root)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return DaemonInfo(
            host=str(raw["host"]),
            port=int(raw["port"]),
            pid=int(raw.get("pid", 0)),
            started_at=str(raw.get("started_at", "")),
            repo_root=str(raw.get("repo_root", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def discover_daemon_url(*, repo_root: Path) -> str | None:
    """Return the daemon URL from env or the repo marker, or None."""
    env_url = os.environ.get("RESEARCH_PLUGIN_DAEMON_URL")
    if env_url:
        return env_url.rstrip("/")
    info = read_marker(repo_root=repo_root)
    if info is None:
        return None
    return info.url
