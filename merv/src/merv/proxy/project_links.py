"""Proxy-local repo_root ↔ project_id links.

The proxy retains the legacy `project_links.sqlite` table shape so machines
linked by pre-proxy releases continue to work. No daemon is involved.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from merv.shared.machine_dirs import resolve_machine_state_dir


_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_links (
  repo_root TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def default_project_links_path(
    *, client_config: dict[str, str] | None = None, config_path: Path | None = None
) -> Path:
    config = client_config or {}
    raw = str(config.get("daemon_state_dir") or "").strip()
    if raw:
        return Path(raw).expanduser() / "project_links.sqlite"
    if config_path is not None:
        return Path(config_path).expanduser().parent / "project_links.sqlite"
    return resolve_machine_state_dir() / "project_links.sqlite"


class ProjectLinks:
    def __init__(self, *, db_path: Path) -> None:
        self.db_path = db_path
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        if not self._initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._initialized = True
        return conn

    def link(self, *, repo_root: str, project_id: str) -> None:
        canonical = str(Path(repo_root).expanduser().resolve())
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO project_links (repo_root, project_id, created_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(repo_root) DO UPDATE SET "
                    "project_id = excluded.project_id",
                    (canonical, project_id, _now_iso()),
                )
        finally:
            conn.close()

    def project_for_repo(self, *, repo_root: str) -> str | None:
        canonical = str(Path(repo_root).expanduser().resolve())
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT project_id FROM project_links WHERE repo_root = ?",
                (canonical,),
            ).fetchone()
        finally:
            conn.close()
        return str(row["project_id"]) if row is not None else None

    def list_links(self) -> list[dict[str, str]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT repo_root, project_id, created_at FROM project_links ORDER BY repo_root"
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "repo_root": str(row["repo_root"]),
                "project_id": str(row["project_id"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def unlink(self, *, repo_root: str) -> bool:
        canonical = str(Path(repo_root).expanduser().resolve())
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM project_links WHERE repo_root = ?", (canonical,)
                )
        finally:
            conn.close()
        return bool(cur.rowcount)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
