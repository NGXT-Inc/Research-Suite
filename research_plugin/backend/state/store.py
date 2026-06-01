"""SQLite state management for Research Plugin v0.0001."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..utils import NotFoundError, ValidationError
from ..utils import new_id
from ..utils import now_iso


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  statement TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  confidence TEXT NOT NULL DEFAULT 'medium',
  created_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_index INTEGER NOT NULL DEFAULT 1,
  revision_context TEXT NOT NULL DEFAULT '',
  conclusion TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS experiment_claims (
  experiment_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  PRIMARY KEY(experiment_id, claim_id)
);

CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  path TEXT NOT NULL,
	  kind TEXT NOT NULL,
	  title TEXT NOT NULL DEFAULT '',
	  current_version_id TEXT,
	  version_token TEXT NOT NULL,
	  mtime_ns INTEGER NOT NULL,
	  size_bytes INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  git_commit TEXT,
  missing INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT 'codex',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
	  UNIQUE(project_id, path),
	  FOREIGN KEY(project_id) REFERENCES projects(id)
	);

	CREATE TABLE IF NOT EXISTS resource_versions (
	  id TEXT PRIMARY KEY,
	  resource_id TEXT NOT NULL,
	  project_id TEXT NOT NULL,
	  path TEXT NOT NULL,
	  content_sha256 TEXT NOT NULL,
	  size_bytes INTEGER NOT NULL,
	  mtime_ns INTEGER NOT NULL,
	  observed_at TEXT NOT NULL,
	  content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
	  created_by TEXT NOT NULL DEFAULT 'codex',
	  created_at TEXT NOT NULL,
	  FOREIGN KEY(resource_id) REFERENCES resources(id),
	  FOREIGN KEY(project_id) REFERENCES projects(id)
	);

	CREATE TABLE IF NOT EXISTS resource_associations (
	  id TEXT PRIMARY KEY,
	  resource_id TEXT NOT NULL,
	  version_id TEXT,
	  target_type TEXT NOT NULL,
	  target_id TEXT NOT NULL,
	  role TEXT NOT NULL,
  attempt_index INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(resource_id, target_type, target_id, role, attempt_index),
	  FOREIGN KEY(resource_id) REFERENCES resources(id),
	  FOREIGN KEY(version_id) REFERENCES resource_versions(id)
	);

CREATE TABLE IF NOT EXISTS review_requests (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  role TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  capability TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  target_snapshot_id TEXT NOT NULL,
  producer_session_id TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS review_sessions (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  declared_agent TEXT NOT NULL DEFAULT '',
  caller_session_id TEXT NOT NULL DEFAULT '',
  independence TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(request_id) REFERENCES review_requests(id)
);

CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  target_snapshot_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  role TEXT NOT NULL,
  verdict TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  findings_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id),
  FOREIGN KEY(request_id) REFERENCES review_requests(id),
  FOREIGN KEY(session_id) REFERENCES review_sessions(id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  type TEXT NOT NULL,
  target_type TEXT NOT NULL DEFAULT '',
  target_id TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  attempt_index INTEGER NOT NULL DEFAULT 0,
  runtime_job_id TEXT,
  backend TEXT NOT NULL DEFAULT 'ray',
  command TEXT NOT NULL,
  cwd TEXT NOT NULL DEFAULT '.',
  expected_outputs_json TEXT NOT NULL DEFAULT '[]',
  backend_hints_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  logs_cache TEXT NOT NULL DEFAULT '',
  error TEXT,
  materialized_at TEXT,
  materialize_error TEXT,
  materialize_attempts INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  progress_phase TEXT NOT NULL DEFAULT '',
  progress_message TEXT NOT NULL DEFAULT '',
  progress_updated_at TEXT,
  sandbox_id TEXT NOT NULL DEFAULT '',
  gpu TEXT NOT NULL DEFAULT '',
  ssh_address TEXT NOT NULL DEFAULT '',
  submitted_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
"""


# Rebuild shape for the legacy `resources` table whose UNIQUE was on `path`
# alone. SQLite cannot drop a column-level UNIQUE in place, so we copy into this
# shape (UNIQUE on project_id + path) and swap. Kept in sync with the resources
# block in SCHEMA above.
_RESOURCES_REBUILD_DDL = """
CREATE TABLE resources_migrate (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  current_version_id TEXT,
  version_token TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL,
  size_bytes INTEGER NOT NULL,
  observed_at TEXT NOT NULL,
  git_commit TEXT,
  missing INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT 'codex',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, path),
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
"""


class StateStore:
    """Owns SQLite connections and basic persistence helpers."""

    def __init__(self, *, db_path: Path, repo_root: Path) -> None:
        self.db_path = db_path
        self.repo_root = repo_root.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets the background reconcile poller read while a submit writes,
        # instead of readers and writers blocking each other (rollback-journal
        # mode upgrades a read lock to write and returns SQLITE_BUSY immediately,
        # which surfaced as "database is locked" on concurrent submits).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            # IMMEDIATE acquires the write lock up front so busy_timeout governs
            # the wait. A DEFERRED BEGIN takes a read lock first and then fails
            # instantly when it can't upgrade under contention.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        self._migrate_resources_unique()
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            self._ensure_forward_schema(conn=conn)
            row = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
            if row is None:
                project_id = new_id(prefix="proj")
                conn.execute(
                    "INSERT INTO projects (id, name, summary, created_at) VALUES (?, ?, ?, ?)",
                    (project_id, "Local Research Project", "", now_iso()),
                )
                self.record_event(
                    conn=conn,
                    project_id=project_id,
                    event_type="project.created",
                    target_type="project",
                    target_id=project_id,
                    payload={"name": "Local Research Project"},
                )
            conn.commit()
        finally:
            conn.close()

    def _ensure_forward_schema(self, *, conn: sqlite3.Connection) -> None:
        self._ensure_columns(
            conn=conn,
            table="jobs",
            columns={
                "progress_phase": "TEXT NOT NULL DEFAULT ''",
                "progress_message": "TEXT NOT NULL DEFAULT ''",
                "progress_updated_at": "TEXT",
                "sandbox_id": "TEXT NOT NULL DEFAULT ''",
                "gpu": "TEXT NOT NULL DEFAULT ''",
                "ssh_address": "TEXT NOT NULL DEFAULT ''",
            },
        )
        # Experiments now persist the accepted conclusion on `complete`; older
        # databases predate the column.
        self._ensure_columns(
            conn=conn,
            table="experiments",
            columns={"conclusion": "TEXT NOT NULL DEFAULT ''"},
        )
        # The shadow-git unplug (May 2026) dropped these columns from the
        # SCHEMA constant, but pre-existing databases still have them. The
        # `snapshot_status` column is NOT NULL with no default, so any INSERT
        # via the new code raises sqlite3.IntegrityError. Drop them on boot
        # so old DBs match the new INSERT shape. Idempotent — _drop_columns
        # is a no-op when the columns are already gone.
        self._drop_columns(
            conn=conn,
            table="resource_versions",
            columns=("snapshot_status", "git_path", "git_commit"),
        )

    def _ensure_columns(
        self,
        *,
        conn: sqlite3.Connection,
        table: str,
        columns: dict[str, str],
    ) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _drop_columns(
        self,
        *,
        conn: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
    ) -> None:
        """Drop columns that no longer appear in the live schema.

        Requires SQLite ≥ 3.35 for `ALTER TABLE ... DROP COLUMN`. Idempotent:
        a column already absent is silently skipped.
        """
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name in columns:
            if name in existing:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")

    def _migrate_resources_unique(self) -> None:
        """Re-key `resources` uniqueness from `path` to `(project_id, path)`.

        The original schema declared `path TEXT NOT NULL UNIQUE`, which blocked
        two projects from registering the same repo-relative file. SQLite cannot
        drop a column-level UNIQUE in place, so detect the legacy autoindex and
        rebuild. No-op once the (project_id, path) unique index already exists
        (fresh databases get the new shape directly from SCHEMA).
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if not self._resources_needs_unique_migration(conn=conn):
                return
            # PRAGMA foreign_keys cannot change inside a transaction; toggle it
            # off around the rebuild so DROP/RENAME don't trip referential checks
            # against resource_versions / resource_associations (both key on id).
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Single statement, so execute() (not executescript(), which would
                # force-commit our BEGIN and break the rebuild's atomicity).
                conn.execute(_RESOURCES_REBUILD_DDL)
                conn.execute(
                    """
                    INSERT INTO resources_migrate (
                      id, project_id, path, kind, title, current_version_id,
                      version_token, mtime_ns, size_bytes, observed_at, git_commit,
                      missing, created_by, created_at, updated_at
                    )
                    SELECT
                      id, project_id, path, kind, title, current_version_id,
                      version_token, mtime_ns, size_bytes, observed_at, git_commit,
                      missing, created_by, created_at, updated_at
                    FROM resources
                    """
                )
                conn.execute("DROP TABLE resources")
                conn.execute("ALTER TABLE resources_migrate RENAME TO resources")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
        finally:
            conn.close()

    def _resources_needs_unique_migration(self, *, conn: sqlite3.Connection) -> bool:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'resources'"
        ).fetchone()
        if table is None:
            return False
        has_path_only = False
        has_project_path = False
        for idx in conn.execute("PRAGMA index_list(resources)").fetchall():
            if not idx["unique"]:
                continue
            cols = [
                str(info["name"])
                for info in conn.execute(
                    f"PRAGMA index_info({idx['name']})"
                ).fetchall()
            ]
            if cols == ["path"]:
                has_path_only = True
            elif cols == ["project_id", "path"]:
                has_project_path = True
        return has_path_only and not has_project_path

    def require_project_id(self, *, conn: sqlite3.Connection, project_id: str | None) -> str:
        if not project_id:
            raise ValidationError("project_id is required")
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"project not found: {project_id}")
        return project_id

    def record_event(
        self,
        *,
        conn: sqlite3.Connection,
        project_id: str,
        event_type: str,
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO events (project_id, type, target_type, target_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, event_type, target_type, target_id, json.dumps(payload or {}, sort_keys=True), now_iso()),
        )


def row_to_dict(*, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(*, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(row=row) or {} for row in rows]
