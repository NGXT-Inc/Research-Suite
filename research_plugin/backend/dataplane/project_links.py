"""Daemon-local repo_root ↔ project_id mapping (cloud plan §3.2, Phase 8).

Project identity decouples from the filesystem: the cloud mints ``project_id``
and never accepts a path. The daemon keeps the ``repo_root ↔ project_id``
mapping locally (the successor of ``directory_projects``, here named
``project_links`` per the plan). The proxy resolves identity via the daemon
(GET /local/route?repo_root=) and sends explicit ``project_id`` on cloud
calls, so ``repo_root`` never crosses the machine boundary.

A small SQLite file under the daemon's ~/.research_plugin so the mapping
survives restarts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..utils import now_iso


_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_links (
  repo_root TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class ProjectLinks:
    """The daemon's repo_root → project_id registry."""

    def __init__(self, *, db_path: Path) -> None:
        self.db_path = db_path
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
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
                    (canonical, project_id, now_iso()),
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
