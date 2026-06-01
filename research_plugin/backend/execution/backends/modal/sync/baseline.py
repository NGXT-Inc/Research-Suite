"""SQLite-backed sync baseline.

One database per repo at .research_plugin/modal/sync.sqlite, holding the
last-confirmed-synced state of every tracked file across all projects.

Schema:

  sync_baseline:
    project_id, path, local_mtime_ns, local_size, remote_mtime_ns, remote_size,
    state ('clean' | 'conflict'), conflict_local_json, conflict_remote_json,
    last_synced_at
    PRIMARY KEY (project_id, path)

  sync_projects:
    project_id, volume_name, mount_path, repo_dir, registered_at, last_polled_at
    PRIMARY KEY (project_id)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from .types import ConflictRecord, FileFingerprint


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sync_baseline (
  project_id        TEXT NOT NULL,
  path              TEXT NOT NULL,
  local_mtime_ns    INTEGER,
  local_size        INTEGER,
  remote_mtime_ns   INTEGER,
  remote_size       INTEGER,
  state             TEXT NOT NULL DEFAULT 'clean',
  conflict_local    TEXT,
  conflict_remote   TEXT,
  last_synced_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_sync_baseline_state
  ON sync_baseline(project_id, state);

CREATE TABLE IF NOT EXISTS sync_projects (
  project_id     TEXT PRIMARY KEY,
  volume_name    TEXT NOT NULL,
  mount_path     TEXT NOT NULL,
  repo_dir       TEXT NOT NULL,
  registered_at  TEXT NOT NULL,
  last_polled_at TEXT
);
"""


class BaselineStore:
    """Owns the sync.sqlite database. Stateless beyond the connection cache."""

    def __init__(self, *, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ---------- project registry ----------

    def register_project(
        self,
        *,
        project_id: str,
        volume_name: str,
        mount_path: str,
        repo_dir: str,
        registered_at: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sync_projects (project_id, volume_name, mount_path, repo_dir, registered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                  volume_name = excluded.volume_name,
                  mount_path = excluded.mount_path,
                  repo_dir = excluded.repo_dir
                """,
                (project_id, volume_name, mount_path, repo_dir, registered_at),
            )

    def known_projects(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT project_id FROM sync_projects ORDER BY registered_at"
            ).fetchall()
            return [str(row["project_id"]) for row in rows]
        finally:
            conn.close()

    def project_info(self, *, project_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sync_projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def mark_polled(self, *, project_id: str, when: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sync_projects SET last_polled_at = ? WHERE project_id = ?",
                (when, project_id),
            )

    # ---------- baseline access ----------

    def load_baseline(
        self, *, project_id: str
    ) -> dict[str, tuple[FileFingerprint | None, FileFingerprint | None]]:
        """Return {path: (local_baseline, remote_baseline)} for clean rows only.

        Conflict rows are excluded from baseline so they don't participate in
        the three-way diff until resolved.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT path, local_mtime_ns, local_size, remote_mtime_ns, remote_size
                FROM sync_baseline
                WHERE project_id = ? AND state = 'clean'
                """,
                (project_id,),
            ).fetchall()
            result: dict[str, tuple[FileFingerprint | None, FileFingerprint | None]] = {}
            for row in rows:
                local = _fingerprint(
                    path=str(row["path"]),
                    mtime_ns=row["local_mtime_ns"],
                    size_bytes=row["local_size"],
                )
                remote = _fingerprint(
                    path=str(row["path"]),
                    mtime_ns=row["remote_mtime_ns"],
                    size_bytes=row["remote_size"],
                )
                result[str(row["path"])] = (local, remote)
            return result
        finally:
            conn.close()

    def conflict_paths(self, *, project_id: str) -> set[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path FROM sync_baseline WHERE project_id = ? AND state = 'conflict'",
                (project_id,),
            ).fetchall()
            return {str(row["path"]) for row in rows}
        finally:
            conn.close()

    def conflicts(self, *, project_id: str) -> list[ConflictRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT path, conflict_local, conflict_remote
                FROM sync_baseline
                WHERE project_id = ? AND state = 'conflict'
                """,
                (project_id,),
            ).fetchall()
            return [
                ConflictRecord(
                    path=str(row["path"]),
                    local=_fingerprint_from_json(row["conflict_local"]),
                    remote=_fingerprint_from_json(row["conflict_remote"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    # ---------- baseline updates ----------

    def upsert_clean(
        self,
        *,
        project_id: str,
        path: str,
        local: FileFingerprint | None,
        remote: FileFingerprint | None,
        synced_at: str,
    ) -> None:
        """Mark path as in-sync with the given fingerprints. None side = absent."""
        if local is None and remote is None:
            self.delete_path(project_id=project_id, path=path)
            return
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sync_baseline (
                  project_id, path, local_mtime_ns, local_size, remote_mtime_ns, remote_size,
                  state, conflict_local, conflict_remote, last_synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'clean', NULL, NULL, ?)
                ON CONFLICT(project_id, path) DO UPDATE SET
                  local_mtime_ns = excluded.local_mtime_ns,
                  local_size = excluded.local_size,
                  remote_mtime_ns = excluded.remote_mtime_ns,
                  remote_size = excluded.remote_size,
                  state = 'clean',
                  conflict_local = NULL,
                  conflict_remote = NULL,
                  last_synced_at = excluded.last_synced_at
                """,
                (
                    project_id,
                    path,
                    local.mtime_ns if local else None,
                    local.size_bytes if local else None,
                    remote.mtime_ns if remote else None,
                    remote.size_bytes if remote else None,
                    synced_at,
                ),
            )

    def mark_conflict(
        self,
        *,
        project_id: str,
        path: str,
        local: FileFingerprint | None,
        remote: FileFingerprint | None,
        when: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sync_baseline (
                  project_id, path, local_mtime_ns, local_size, remote_mtime_ns, remote_size,
                  state, conflict_local, conflict_remote, last_synced_at
                )
                VALUES (?, ?, NULL, NULL, NULL, NULL, 'conflict', ?, ?, ?)
                ON CONFLICT(project_id, path) DO UPDATE SET
                  state = 'conflict',
                  conflict_local = excluded.conflict_local,
                  conflict_remote = excluded.conflict_remote,
                  last_synced_at = excluded.last_synced_at
                """,
                (
                    project_id,
                    path,
                    _fingerprint_json(local),
                    _fingerprint_json(remote),
                    when,
                ),
            )

    def delete_path(self, *, project_id: str, path: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM sync_baseline WHERE project_id = ? AND path = ?",
                (project_id, path),
            )


def _fingerprint(
    *, path: str, mtime_ns: object, size_bytes: object
) -> FileFingerprint | None:
    if mtime_ns is None or size_bytes is None:
        return None
    return FileFingerprint(path=path, mtime_ns=int(mtime_ns), size_bytes=int(size_bytes))


def _fingerprint_json(fp: FileFingerprint | None) -> str | None:
    if fp is None:
        return None
    return json.dumps(asdict(fp), sort_keys=True)


def _fingerprint_from_json(raw: object) -> FileFingerprint | None:
    if not raw:
        return None
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return FileFingerprint(
            path=str(data["path"]),
            mtime_ns=int(data["mtime_ns"]),
            size_bytes=int(data["size_bytes"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
