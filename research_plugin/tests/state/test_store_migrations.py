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

        store = StateStore(db_path=self.db, repo_root=self.repo)
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
        StateStore(db_path=self.db, repo_root=self.repo)
        store = StateStore(db_path=self.db, repo_root=self.repo)
        conn = store.connect()
        try:
            uniques = self._unique_index_columns(conn)
            self.assertIn(["project_id", "path"], uniques)
            self.assertNotIn(["path"], uniques)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
