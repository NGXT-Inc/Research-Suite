from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.state.store import StateStore


OLD_SCHEMA = """
CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE resources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
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
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
"""

# Pre-Phase-6 `resource_versions` shape: no created_seq column — ordering
# leaned on SQLite's implicit rowid.
OLD_RESOURCE_VERSIONS_SCHEMA = """
CREATE TABLE resource_versions (
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
"""

# Pre-split `sandboxes` shape: machine-local columns (key_path,
# local_sync_dir) still lived on the cloud-bound row.
OLD_SANDBOXES_SCHEMA = """
CREATE TABLE sandboxes (
  experiment_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  sandbox_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'none',
  gpu TEXT NOT NULL DEFAULT '',
  cpu REAL NOT NULL DEFAULT 0,
  memory INTEGER NOT NULL DEFAULT 0,
  time_limit INTEGER NOT NULL DEFAULT 0,
  ssh_host TEXT NOT NULL DEFAULT '',
  ssh_port INTEGER NOT NULL DEFAULT 0,
  ssh_user TEXT NOT NULL DEFAULT 'root',
  key_path TEXT NOT NULL DEFAULT '',
  workdir TEXT NOT NULL DEFAULT '',
  local_sync_dir TEXT NOT NULL DEFAULT '',
  volume_name TEXT NOT NULL DEFAULT '',
  requested_at TEXT,
  expires_at TEXT,
  last_seen_at TEXT,
  terminated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id)
);
"""


class StoreMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.db = self.repo / ".research_plugin" / "state.sqlite"
        self.db.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_legacy_db(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO projects (id, name, summary, created_at) VALUES (?, ?, ?, ?)",
                ("proj_old", "Legacy", "", "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                """
                INSERT INTO resources (
                  id, project_id, path, kind, title, current_version_id,
                  version_token, mtime_ns, size_bytes, observed_at, git_commit,
                  missing, created_by, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, '', NULL, ?, ?, ?, ?, NULL, 0, 'codex', ?, ?)
                """,
                (
                    "res_old",
                    "proj_old",
                    "shared.md",
                    "note",
                    "shared.md:1:5",
                    1,
                    5,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _unique_index_columns(self, conn: sqlite3.Connection) -> list[list[str]]:
        uniques: list[list[str]] = []
        for idx in conn.execute("PRAGMA index_list(resources)").fetchall():
            if not idx["unique"]:
                continue
            cols = [
                str(info["name"])
                for info in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
            ]
            uniques.append(cols)
        return uniques

    def test_legacy_path_unique_is_rekeyed_to_project_path(self) -> None:
        self._seed_legacy_db()

        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            uniques = self._unique_index_columns(conn)
            self.assertIn(["project_id", "path"], uniques)
            self.assertNotIn(["path"], uniques)
            row = conn.execute("SELECT id, project_id, path, deleted FROM resources").fetchone()
            self.assertEqual(row["id"], "res_old")
            self.assertEqual(row["path"], "shared.md")
            self.assertEqual(row["deleted"], 0)
        finally:
            conn.close()

        # After migration, a different project can register the same repo file.
        app = ResearchPluginApp(repo_root=self.repo, db_path=self.db)
        (self.repo / "shared.md").write_text("hello\n")
        new_project = app.call_tool("project.create", {"name": "New"})
        res = app.call_tool(
            "resource.register_file",
            {"project_id": new_project["id"], "path": "shared.md"},
        )
        self.assertTrue(res["id"])
        self.assertNotEqual(res["id"], "res_old")

    def test_fresh_db_is_not_rebuilt(self) -> None:
        # A brand-new store already has the (project_id, path) unique index, so the
        # migration must be a no-op (idempotent on repeated construction).
        StateStore(db_path=self.db)
        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            uniques = self._unique_index_columns(conn)
            self.assertIn(["project_id", "path"], uniques)
            self.assertNotIn(["path"], uniques)
        finally:
            conn.close()

    def _sandbox_columns(self, conn: sqlite3.Connection) -> set[str]:
        return {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sandboxes)").fetchall()
        }

    def test_machine_local_sandbox_columns_are_dropped(self) -> None:
        # Cloud-split Phase 3: key_path / local_sync_dir moved to the worker's
        # local store; an upgraded database loses the columns but keeps every
        # provider-portable fact on the row.
        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(OLD_SANDBOXES_SCHEMA)
            conn.execute(
                """
                INSERT INTO sandboxes (
                  experiment_id, project_id, sandbox_id, status, ssh_host,
                  ssh_port, key_path, local_sync_dir, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "exp_old",
                    "proj_old",
                    "sb-1",
                    "terminated",
                    "host.example",
                    2222,
                    "/keys/exp_old",
                    "/repo/experiments/exp_old",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            columns = self._sandbox_columns(conn)
            self.assertNotIn("key_path", columns)
            self.assertNotIn("local_sync_dir", columns)
            row = conn.execute("SELECT * FROM sandboxes").fetchone()
            self.assertEqual(row["experiment_id"], "exp_old")
            self.assertEqual(row["status"], "terminated")
            self.assertEqual(row["ssh_host"], "host.example")
            self.assertEqual(row["ssh_port"], 2222)
        finally:
            conn.close()

        # Idempotent: a second boot with the columns already gone is a no-op.
        StateStore(db_path=self.db)

    def test_fresh_db_has_no_machine_local_sandbox_columns(self) -> None:
        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            columns = self._sandbox_columns(conn)
            self.assertNotIn("key_path", columns)
            self.assertNotIn("local_sync_dir", columns)
        finally:
            conn.close()

    def test_legacy_db_gains_tenant_and_created_seq_columns(self) -> None:
        # Cloud-split Phase 6: tenancy lands on projects (the fixed 'local'
        # tenant), and the explicit ordering column that replaced rowid
        # ordering backfills FROM rowid — so the order historical queries
        # observed is preserved exactly across the upgrade.
        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(OLD_RESOURCE_VERSIONS_SCHEMA)
            for suffix in ("a", "b", "c"):
                conn.execute(
                    """
                    INSERT INTO resource_versions (
                      id, resource_id, project_id, path, content_sha256,
                      size_bytes, mtime_ns, observed_at, created_by, created_at
                    )
                    VALUES (?, 'res_old', 'proj_old', 'shared.md', ?, 1, 1,
                            '2026-01-01T00:00:00Z', 'codex', '2026-01-01T00:00:00Z')
                    """,
                    (f"rver_{suffix}", f"sha_{suffix}"),
                )
            conn.commit()
        finally:
            conn.close()

        StateStore(db_path=self.db)  # converge, then re-boot for idempotence
        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            row = conn.execute(
                "SELECT tenant_id FROM projects WHERE id = 'proj_old'"
            ).fetchone()
            self.assertEqual(row["tenant_id"], "local")
            rows = conn.execute(
                "SELECT id, created_seq FROM resource_versions ORDER BY created_seq"
            ).fetchall()
            self.assertEqual([r["id"] for r in rows], ["rver_a", "rver_b", "rver_c"])
            self.assertEqual([r["created_seq"] for r in rows], [1, 2, 3])
        finally:
            conn.close()

    def test_legacy_db_gains_phase5_columns_and_metrics_records(self) -> None:
        # Cloud-split Phase 5: the management-key reference and the expiry
        # parachute record join the sandboxes row, and metrics snapshots gain
        # a control-plane record table — all via additive convergence on a
        # pre-Phase-5 database.
        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(OLD_SANDBOXES_SCHEMA)
            conn.commit()
        finally:
            conn.close()

        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            columns = self._sandbox_columns(conn)
            for column in (
                "mgmt_key_ref",
                "parachute_state",
                "parachute_object_key",
                "parachute_sha256",
                "parachute_size_bytes",
                "parachute_expires_at",
            ):
                self.assertIn(column, columns)
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("metrics_snapshots", tables)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
