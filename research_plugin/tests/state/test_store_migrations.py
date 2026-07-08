from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.state.store import MIGRATIONS, StateStore


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
        app = TestBrain(repo_root=self.repo, db_path=self.db)
        (self.repo / "shared.md").write_text("hello\n")
        new_project = app.call_tool("project.create", {"name": "New"})
        res = app.call_tool(
            "resource.register",
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

    def test_legacy_syntheses_table_is_renamed_to_reflections(self) -> None:
        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(
                """
                CREATE TABLE syntheses (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  title TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  attempt_index INTEGER NOT NULL DEFAULT 1,
                  revision_context TEXT NOT NULL DEFAULT '',
                  roster_json TEXT NOT NULL DEFAULT '[]',
                  corpus_json TEXT NOT NULL DEFAULT '{}',
                  published_at TEXT,
                  published_graph_version_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  created_seq INTEGER NOT NULL DEFAULT 0,
                  FOREIGN KEY(project_id) REFERENCES projects(id)
                );
                """
            )
            conn.execute(
                """
                INSERT INTO syntheses (
                  id, project_id, title, status, attempt_index, revision_context,
                  roster_json, corpus_json, created_at, updated_at, created_seq
                )
                VALUES (
                  'syn_legacy', 'proj_old', 'Legacy reflection', 'reflecting',
                  2, 'needs another pass', '[]', '{"claims": []}',
                  '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', 7
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        StateStore(db_path=self.db)  # converge, then re-boot for idempotence
        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("reflections", tables)
            self.assertNotIn("syntheses", tables)
            row = conn.execute(
                """
                SELECT id, project_id, title, status, attempt_index,
                       revision_context, corpus_json, created_seq
                FROM reflections
                WHERE id = 'syn_legacy'
                """
            ).fetchone()
            self.assertEqual(row["project_id"], "proj_old")
            self.assertEqual(row["title"], "Legacy reflection")
            self.assertEqual(row["status"], "reflecting")
            self.assertEqual(row["attempt_index"], 2)
            self.assertEqual(row["revision_context"], "needs another pass")
            self.assertEqual(row["corpus_json"], '{"claims": []}')
            self.assertEqual(row["created_seq"], 7)
            migration = conn.execute(
                "SELECT name FROM schema_migrations WHERE version = 15"
            ).fetchone()
            self.assertEqual(migration["name"], "rename_syntheses_to_reflections")
        finally:
            conn.close()

    def test_legacy_sandboxes_gain_last_command_columns(self) -> None:
        # Migration 16: the last_command_* family reached the fresh SCHEMA
        # without a migration; migrated deployments 500ed on the sandbox
        # signal ETag. Seed the pre-command-snapshot shape and converge.
        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(OLD_SANDBOXES_SCHEMA)
            conn.execute(
                """
                INSERT INTO sandboxes (
                  experiment_id, project_id, status, created_at, updated_at
                )
                VALUES ('exp_old', 'proj_old', 'running',
                        '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')
                """
            )
            conn.commit()
        finally:
            conn.close()

        StateStore(db_path=self.db)  # converge, then re-boot for idempotence
        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(sandboxes)").fetchall()
            }
            self.assertTrue(
                set(StateStore.SANDBOX_LAST_COMMAND_COLUMNS) <= columns,
                sorted(set(StateStore.SANDBOX_LAST_COMMAND_COLUMNS) - columns),
            )
        finally:
            conn.close()
        # The query that exposed the drift must run against the migrated shape.
        signal = store.project_sandbox_signal(project_id="proj_old")
        self.assertIsInstance(signal, str)

    def test_storage_missing_status_migrates_to_expired(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(
                """
                CREATE TABLE projects (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  summary TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'active',
                  hard_stop_reflection_id TEXT,
                  hard_stop_rationale TEXT NOT NULL DEFAULT '',
                  stopped_at TEXT,
                  tenant_id TEXT NOT NULL DEFAULT 'local',
                  created_at TEXT NOT NULL
                );
                CREATE TABLE storage_objects (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL,
                  content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                  namespace TEXT NOT NULL,
                  status TEXT NOT NULL,
                  upload_id TEXT,
                  expires_at TEXT,
                  created_by TEXT NOT NULL DEFAULT 'codex',
                  producing_experiment_id TEXT NOT NULL DEFAULT '',
                  producing_run TEXT NOT NULL DEFAULT '',
                  source_uri TEXT NOT NULL DEFAULT '',
                  notes TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  last_accessed_at TEXT,
                  created_seq INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(project_id, name, version),
                  FOREIGN KEY(project_id) REFERENCES projects(id)
                );
                CREATE TABLE schema_migrations (
                  version INTEGER PRIMARY KEY,
                  name TEXT NOT NULL,
                  applied_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO projects (id, name, summary, created_at)
                VALUES ('proj_old', 'Legacy', '', '2026-01-01T00:00:00Z')
                """
            )
            conn.execute(
                """
                INSERT INTO storage_objects (
                  id, project_id, name, version, kind, content_sha256, size_bytes,
                  content_type, namespace, status, created_at, updated_at
                )
                VALUES (
                  'obj_missing', 'proj_old', 'old.bin', 1, 'dataset',
                  'abc123', 3, 'application/octet-stream', 'proj_old', 'missing',
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (?, ?, '2026-01-01T00:00:00Z')
                """,
                [(version, name) for version, name, _ in MIGRATIONS if version < 10],
            )
            conn.commit()
        finally:
            conn.close()

        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            row = conn.execute(
                "SELECT status FROM storage_objects WHERE id = 'obj_missing'"
            ).fetchone()
            self.assertEqual(row["status"], "expired")
            migration = conn.execute(
                "SELECT name FROM schema_migrations WHERE version = 10"
            ).fetchone()
            self.assertEqual(migration["name"], "normalize_storage_missing_status")
        finally:
            conn.close()

    def _sandbox_columns(self, conn: sqlite3.Connection) -> set[str]:
        return {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sandboxes)").fetchall()
        }

    def _sandbox_unique_index_columns(self, conn: sqlite3.Connection) -> list[list[str]]:
        uniques: list[list[str]] = []
        for idx in conn.execute("PRAGMA index_list(sandboxes)").fetchall():
            if not idx["unique"]:
                continue
            columns = [
                str(info["name"])
                for info in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
            ]
            uniques.append(columns)
        return uniques

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
            self.assertNotIn("experiment_id", columns)
            row = conn.execute("SELECT * FROM sandboxes").fetchone()
            self.assertEqual(row["status"], "terminated")
            self.assertEqual(row["ssh_host"], "host.example")
            self.assertEqual(row["ssh_port"], 2222)
            attachment = conn.execute(
                "SELECT experiment_id FROM sandbox_attachments WHERE sandbox_uid = ?",
                (row["sandbox_uid"],),
            ).fetchone()
            self.assertEqual(attachment["experiment_id"], "exp_old")
        finally:
            conn.close()

        # Idempotent: a second boot with the columns already gone is a no-op.
        StateStore(db_path=self.db)

    def test_legacy_sandboxes_gain_uid_and_attachments(self) -> None:
        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(OLD_SANDBOXES_SCHEMA)
            for experiment_id, status, terminated_at in (
                ("exp_run", "running", None),
                ("exp_done", "terminated", "2026-01-01T02:00:00Z"),
            ):
                conn.execute(
                    """
                    INSERT INTO sandboxes (
                      experiment_id, project_id, sandbox_id, status,
                      terminated_at, created_at, updated_at
                    )
                    VALUES (?, 'proj_old', ?, ?, ?, ?, ?)
                    """,
                    (
                        experiment_id,
                        f"sb-{experiment_id}",
                        status,
                        terminated_at,
                        "2026-01-01T00:00:00Z",
                        "2026-01-01T01:00:00Z",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        StateStore(db_path=self.db)
        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            pk = [
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(sandboxes)").fetchall()
                if int(row["pk"] or 0) > 0
            ]
            self.assertEqual(pk, ["sandbox_uid"])
            rows = conn.execute(
                """
                SELECT s.sandbox_uid, a.experiment_id
                FROM sandboxes s
                JOIN sandbox_attachments a ON a.sandbox_uid = s.sandbox_uid
                ORDER BY a.experiment_id
                """
            ).fetchall()
            self.assertEqual([row["experiment_id"] for row in rows], ["exp_done", "exp_run"])
            uids = [str(row["sandbox_uid"]) for row in rows]
            self.assertEqual(len(set(uids)), 2)
            for sandbox_uid in uids:
                self.assertEqual(len(sandbox_uid), 32)
                int(sandbox_uid, 16)
            attachments = conn.execute(
                """
                SELECT sandbox_uid, experiment_id, detached_at
                FROM sandbox_attachments
                ORDER BY experiment_id
                """
            ).fetchall()
            self.assertEqual(
                [(row["sandbox_uid"], row["experiment_id"]) for row in attachments],
                [(rows[0]["sandbox_uid"], "exp_done"), (rows[1]["sandbox_uid"], "exp_run")],
            )
            self.assertEqual(attachments[0]["detached_at"], "2026-01-01T02:00:00Z")
            self.assertIsNone(attachments[1]["detached_at"])
        finally:
            conn.close()

    def test_sandboxes_experiment_unique_is_dropped(self) -> None:
        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(
                """
                CREATE TABLE sandboxes (
                  sandbox_uid TEXT PRIMARY KEY,
                  experiment_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  sandbox_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'none',
                  requested_at TEXT,
                  terminated_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  created_seq INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(experiment_id)
                );
                CREATE TABLE sandbox_attachments (
                  sandbox_uid TEXT NOT NULL,
                  experiment_id TEXT NOT NULL,
                  attached_at TEXT NOT NULL,
                  detached_at TEXT,
                  PRIMARY KEY (sandbox_uid, experiment_id)
                );
                """
            )
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, experiment_id, project_id, sandbox_id, status,
                  created_at, updated_at, created_seq
                )
                VALUES (
                  'uid_old', 'exp_parallel', 'proj_old', 'sb-old', 'running',
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                VALUES ('uid_old', 'exp_parallel', '2026-01-01T00:00:00Z', NULL)
                """
            )
            conn.commit()
        finally:
            conn.close()

        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            self.assertNotIn(
                ["experiment_id"], self._sandbox_unique_index_columns(conn)
            )
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, project_id, sandbox_id, status,
                  created_at, updated_at, created_seq
                )
                VALUES (
                  'uid_new', 'proj_old', 'sb-new', 'running',
                  '2026-01-01T00:00:01Z', '2026-01-01T00:00:01Z', 2
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                VALUES ('uid_new', 'exp_parallel', '2026-01-01T00:00:01Z', NULL)
                """
            )
            rows = conn.execute(
                """
                SELECT s.sandbox_uid
                FROM sandboxes s
                JOIN sandbox_attachments a ON a.sandbox_uid = s.sandbox_uid
                WHERE a.experiment_id = ?
                ORDER BY s.created_seq
                """,
                ("exp_parallel",),
            ).fetchall()
            self.assertEqual([row["sandbox_uid"] for row in rows], ["uid_old", "uid_new"])
            attachment = conn.execute(
                "SELECT detached_at FROM sandbox_attachments WHERE sandbox_uid = 'uid_old'"
            ).fetchone()
            self.assertIsNone(attachment["detached_at"])
        finally:
            conn.close()

    def test_sandbox_attachments_rebuild_allows_history_rows(self) -> None:
        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(
                """
                CREATE TABLE sandboxes (
                  sandbox_uid TEXT PRIMARY KEY,
                  experiment_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  sandbox_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'none',
                  requested_at TEXT,
                  terminated_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE sandbox_attachments (
                  sandbox_uid TEXT NOT NULL,
                  experiment_id TEXT NOT NULL,
                  attached_at TEXT NOT NULL,
                  detached_at TEXT,
                  PRIMARY KEY (sandbox_uid, experiment_id)
                );
                INSERT INTO sandboxes (
                  sandbox_uid, experiment_id, project_id, sandbox_id, status,
                  created_at, updated_at
                )
                VALUES (
                  'uid_old', 'exp_parallel', 'proj_old', 'sb-old', 'running',
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                );
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                VALUES ('uid_old', 'exp_parallel', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z');
                """
            )
            conn.commit()
        finally:
            conn.close()

        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            pk = [
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(sandbox_attachments)").fetchall()
                if int(row["pk"] or 0) > 0
            ]
            self.assertEqual(pk, [])
            conn.execute(
                """
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                VALUES ('uid_old', 'exp_parallel', '2026-01-01T02:00:00Z', NULL)
                """
            )
            rows = conn.execute(
                """
                SELECT detached_at FROM sandbox_attachments
                WHERE sandbox_uid = 'uid_old' AND experiment_id = 'exp_parallel'
                ORDER BY attached_at
                """
            ).fetchall()
            self.assertEqual([row["detached_at"] for row in rows], ["2026-01-01T01:00:00Z", None])
        finally:
            conn.close()

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

    def test_fresh_db_has_phase7_tables_and_columns(self) -> None:
        # Cloud-split Phase 7: identity + cost-governance schema lands on fresh
        # DBs, the reviewer capability is hashed, and sandboxes record price.
        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table in (
                "tenants",
                "tenant_quotas",
                "sandbox_generations",
            ):
                self.assertIn(table, tables)
            rr_cols = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(review_requests)").fetchall()
            }
            self.assertIn("capability_hash", rr_cols)
            self.assertNotIn("capability", rr_cols)
            rs_cols = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(review_sessions)").fetchall()
            }
            self.assertIn("tenant_id", rs_cols)
            self.assertIn("price_usd_per_hour", self._sandbox_columns(conn))
        finally:
            conn.close()

    def test_legacy_plaintext_capability_is_rehashed(self) -> None:
        # Cloud-split Phase 7: the plaintext, column-level-UNIQUE `capability`
        # column is rebuilt to `capability_hash` (= sha256 of the plaintext),
        # so an already-issued token still resolves; the table is rebuilt
        # because SQLite cannot drop a UNIQUE column in place.
        import hashlib

        self._seed_legacy_db()
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(
                """
                CREATE TABLE review_requests (
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
                CREATE TABLE review_sessions (
                  id TEXT PRIMARY KEY,
                  request_id TEXT NOT NULL,
                  declared_agent TEXT NOT NULL DEFAULT '',
                  caller_session_id TEXT NOT NULL DEFAULT '',
                  independence TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(request_id) REFERENCES review_requests(id)
                );
                """
            )
            conn.execute(
                """
                INSERT INTO review_requests (
                  id, project_id, target_type, target_id, role, capability,
                  status, target_snapshot_id, expires_at, created_at
                )
                VALUES ('rr_old', 'proj_old', 'experiment', 'exp1',
                        'design_reviewer', 'rp_legacy_token', 'requested',
                        'snap', '2099-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )
            conn.commit()
        finally:
            conn.close()

        StateStore(db_path=self.db)  # converge, then re-boot for idempotence
        store = StateStore(db_path=self.db)
        conn = store.connect()
        try:
            cols = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(review_requests)").fetchall()
            }
            self.assertNotIn("capability", cols)
            self.assertIn("capability_hash", cols)
            row = conn.execute(
                "SELECT capability_hash FROM review_requests WHERE id = 'rr_old'"
            ).fetchone()
            self.assertEqual(
                row["capability_hash"],
                hashlib.sha256(b"rp_legacy_token").hexdigest(),
            )
        finally:
            conn.close()

    def test_legacy_db_gains_mgmt_key_and_drops_metrics_records(self) -> None:
        # Management-key references stay on sandbox rows; obsolete sandbox
        # MLflow snapshot records are removed.
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
            self.assertIn("mgmt_key_ref", columns)
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertNotIn("metrics_snapshots", tables)
        finally:
            conn.close()

    def test_legacy_sandboxes_gain_heartbeat_columns(self) -> None:
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
            self.assertIn("idle_since", columns)
            self.assertIn("heartbeat_snapshot_json", columns)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
