"""Dual-dialect record-store tests against a dockerized Postgres (Phase 6).

The exit criterion of cloud plan Phase 6: the SAME service code passes the
record-layer suites on SQLite (the 442-test local baseline) and on Postgres.
This module supplies the Postgres half:

  (a) the translated SCHEMA + the ordered ledger apply cleanly;
  (b) the control-plane contract scenarios (the full research loop, driven
      only through the tool surface) pass against a ResearchPluginApp whose
      store is ``PostgresStateStore`` — services unchanged;
  (c) events identity ordering, resource_versions/associations created_seq
      ordering, ON CONFLICT upsert paths, and the record_event/recent_events
      round trip behave exactly as on SQLite;
  (d) two concurrent transactions serialize (the advisory-lock emulation of
      SQLite's BEGIN IMMEDIATE single-writer semantics).

A ``postgres:16-alpine`` container is started once per module on a random
host port and torn down at module exit. Everything docker-dependent skips
cleanly (fast ``docker info`` probe) when docker is unavailable; the schema
parity tests at the bottom need no docker and always run.

Scope note (per the plan): the behavioral pass covers the record services —
projects/claims/experiments/resources/reviews/syntheses/workflow. Sandbox
rows share the dialect-neutral SQL (created_seq, no rowid) but their
behavioral parity rides with Phase 8's split-mode composition, which is when
a control plane actually serves them.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.config import build_state_store, resolve_db_url
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.metrics_records import MetricsSnapshotStore
from backend.state.dialects import PostgresStateStore, translate_schema_to_postgres
from backend.state.store import SCHEMA, StateStore, next_created_seq
from backend.utils import ValidationError, now_iso
from tests.fakes import FakeRsyncSyncer
from tests.surface.test_control_plane_contract import (
    ClientHarness,
    ControlPlaneContractScenarios,
    InProcessControlPlaneClient,
)


CONTAINER = "rp-test-postgres-dialect"
PASSWORD = "rp-test-pg"

_dsn: str | None = None


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


HAVE_DOCKER = _docker_available()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def setUpModule() -> None:
    """Start one postgres:16-alpine container for the whole module."""
    global _dsn
    if not HAVE_DOCKER:
        return
    port = _free_port()
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            CONTAINER,
            "-e",
            f"POSTGRES_PASSWORD={PASSWORD}",
            "-p",
            f"127.0.0.1:{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    dsn = f"postgresql://postgres:{PASSWORD}@127.0.0.1:{port}/postgres"
    import psycopg

    deadline = time.monotonic() + 60
    while True:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            break
        except psycopg.Error:
            if time.monotonic() > deadline:
                subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
                raise unittest.SkipTest("postgres container never became ready")
            time.sleep(0.5)
    _dsn = dsn


def tearDownModule() -> None:
    if HAVE_DOCKER:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def _reset_database() -> str:
    """A clean public schema in the module's database; returns the DSN."""
    assert _dsn is not None
    import psycopg

    with psycopg.connect(_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    return _dsn


def _postgres_harness() -> ClientHarness:
    """The contract harness, rewired onto a fresh Postgres-backed app."""
    tmp = tempfile.TemporaryDirectory()
    repo = Path(tmp.name)
    app = ResearchPluginApp(
        repo_root=repo,
        db_path=repo / ".research_plugin" / "unused.sqlite",
        execution_backend=FakeSandboxBackend(),
        rsync_syncer=FakeRsyncSyncer(),
        store=PostgresStateStore(dsn=_reset_database()),
    )
    return ClientHarness(
        client=InProcessControlPlaneClient(app=app),
        repo=repo,
        _closers=[app.shutdown, tmp.cleanup],
    )


@unittest.skipUnless(HAVE_DOCKER, "docker unavailable")
class PostgresControlPlaneContractTest(
    ControlPlaneContractScenarios, unittest.TestCase
):
    """(b) The Phase 3 scenario corpus, services unchanged, store swapped."""

    harness_factory = staticmethod(_postgres_harness)


@unittest.skipUnless(HAVE_DOCKER, "docker unavailable")
class PostgresStoreBehaviorTest(unittest.TestCase):
    """(a), (c), (d): schema/ledger application and record-layer semantics."""

    def setUp(self) -> None:
        self.store = PostgresStateStore(dsn=_reset_database())

    def _seed_project(self, project_id: str = "proj_pg") -> str:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, summary, created_at) VALUES (?, ?, ?, ?)",
                (project_id, "PG Project", "", now_iso()),
            )
        return project_id

    def test_schema_and_ledger_apply_cleanly_and_idempotently(self) -> None:
        conn = self.store.connect()
        try:
            tables = {
                str(row["table_name"])
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ).fetchall()
            }
            for table in _sqlite_schema_tables():
                self.assertIn(table, tables)
            ledger = conn.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(
                [(int(r["version"]), str(r["name"])) for r in ledger],
                [(1, "drop_legacy_jobs_table")],
            )
            # No default-project bootstrap: that is local-mode-only behavior.
            count = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
            self.assertEqual(int(count["n"]), 0)
        finally:
            conn.close()
        # Re-construction against the same database is a no-op (IF NOT EXISTS
        # DDL + already-recorded ledger), exactly like the SQLite store.
        PostgresStateStore(dsn=_dsn)

    def test_projects_default_to_the_local_tenant(self) -> None:
        project_id = self._seed_project()
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT tenant_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            self.assertEqual(row["tenant_id"], "local")
        finally:
            conn.close()

    def test_event_ids_are_identity_assigned_and_order_recent_events(self) -> None:
        project_id = self._seed_project()
        with self.store.transaction() as conn:
            for index in range(3):
                self.store.record_event(
                    conn=conn,
                    project_id=project_id,
                    event_type=f"step.{index}",
                    payload={"index": index},
                )
        events = self.store.recent_events(project_id=project_id, limit=10)["events"]
        self.assertEqual([e["type"] for e in events], ["step.2", "step.1", "step.0"])
        self.assertEqual([e["payload"]["index"] for e in events], [2, 1, 0])
        ids = [int(e["id"]) for e in events]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_created_seq_orders_versions_and_associations(self) -> None:
        project_id = self._seed_project()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO resources (
                  id, project_id, path, kind, title, version_token, mtime_ns,
                  size_bytes, observed_at, missing, created_by, created_at, updated_at
                )
                VALUES (?, ?, 'notes.md', 'note', '', 'tok', 1, 1, ?, 0, 'codex', ?, ?)
                """,
                ("res_1", project_id, now_iso(), now_iso(), now_iso()),
            )
            for index in range(3):
                seq = next_created_seq(conn=conn, table="resource_versions")
                self.assertEqual(seq, index + 1)
                conn.execute(
                    """
                    INSERT INTO resource_versions (
                      id, resource_id, project_id, path, content_sha256, size_bytes,
                      mtime_ns, observed_at, created_by, created_at, created_seq
                    )
                    VALUES (?, ?, ?, 'notes.md', ?, 1, 1, ?, 'codex', ?, ?)
                    """,
                    (
                        f"rver_{index}",
                        "res_1",
                        project_id,
                        f"sha{index}",
                        now_iso(),
                        now_iso(),
                        seq,
                    ),
                )
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM resource_versions WHERE resource_id = ? ORDER BY created_seq",
                ("res_1",),
            ).fetchall()
            self.assertEqual(
                [r["id"] for r in rows], ["rver_0", "rver_1", "rver_2"]
            )
        finally:
            conn.close()

    def _seed_resource_with_versions(self, *, project_id: str) -> None:
        """res_1 with two pinned versions (rver_a, rver_b) — FK targets."""
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO resources (
                  id, project_id, path, kind, title, version_token, mtime_ns,
                  size_bytes, observed_at, missing, created_by, created_at, updated_at
                )
                VALUES (?, ?, 'plan.md', 'plan', '', 'tok', 1, 1, ?, 0, 'codex', ?, ?)
                """,
                ("res_1", project_id, now_iso(), now_iso(), now_iso()),
            )
            for version_id in ("rver_a", "rver_b"):
                conn.execute(
                    """
                    INSERT INTO resource_versions (
                      id, resource_id, project_id, path, content_sha256, size_bytes,
                      mtime_ns, observed_at, created_by, created_at, created_seq
                    )
                    VALUES (?, 'res_1', ?, 'plan.md', ?, 1, 1, ?, 'codex', ?, ?)
                    """,
                    (
                        version_id,
                        project_id,
                        f"sha-{version_id}",
                        now_iso(),
                        now_iso(),
                        next_created_seq(conn=conn, table="resource_versions"),
                    ),
                )

    def test_association_upsert_replaces_pin_and_keeps_created_seq(self) -> None:
        """The resource.associate ON CONFLICT path: re-associating the same
        (resource, target, role, attempt) updates the pinned version but keeps
        the original insertion order — rowid parity."""
        project_id = self._seed_project()
        self._seed_resource_with_versions(project_id=project_id)
        insert = """
            INSERT INTO resource_associations
              (id, resource_id, version_id, target_type, target_id, role,
               attempt_index, created_at, created_seq)
            VALUES (?, ?, ?, 'experiment', 'exp_1', 'plan', 1, ?, ?)
            ON CONFLICT(resource_id, target_type, target_id, role, attempt_index)
            DO UPDATE SET version_id = excluded.version_id, created_at = excluded.created_at
        """
        with self.store.transaction() as conn:
            conn.execute(
                insert,
                ("assoc_a", "res_1", "rver_a", now_iso(),
                 next_created_seq(conn=conn, table="resource_associations")),
            )
        with self.store.transaction() as conn:
            conn.execute(
                insert,
                ("assoc_b", "res_1", "rver_b", now_iso(),
                 next_created_seq(conn=conn, table="resource_associations")),
            )
        conn = self.store.connect()
        try:
            rows = conn.execute("SELECT * FROM resource_associations").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "assoc_a")  # original row survived
            self.assertEqual(rows[0]["version_id"], "rver_b")  # pin replaced
            self.assertEqual(int(rows[0]["created_seq"]), 1)  # order kept
        finally:
            conn.close()

    def test_metrics_snapshot_upsert_is_latest_wins(self) -> None:
        """A record service's ON CONFLICT upsert, exercised end to end."""
        project_id = self._seed_project()
        snapshots = MetricsSnapshotStore(store=self.store)
        snapshots.record(
            experiment_id="exp_1",
            project_id=project_id,
            snapshot={"captured_at": "2026-06-01T00:00:00Z", "source": "a", "runs": 1},
        )
        snapshots.record(
            experiment_id="exp_1",
            project_id=project_id,
            snapshot={"captured_at": "2026-06-02T00:00:00Z", "source": "b", "runs": 2},
        )
        loaded = snapshots.load(experiment_id="exp_1")
        self.assertEqual(loaded["runs"], 2)
        self.assertEqual(loaded["captured_at"], "2026-06-02T00:00:00Z")
        conn = self.store.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM metrics_snapshots"
            ).fetchone()
            self.assertEqual(int(count["n"]), 1)
        finally:
            conn.close()

    def test_concurrent_transactions_serialize_on_the_advisory_lock(self) -> None:
        """(d) Both writers run MAX+1 read-modify-write; without single-writer
        semantics they'd both read 0 and the table would end at 1."""
        self._seed_project()
        conn = self.store.connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS txn_probe (n BIGINT NOT NULL)")
        finally:
            conn.close()
        first_in_tx = threading.Event()
        errors: list[Exception] = []

        def writer(*, wait_inside: bool) -> None:
            try:
                with self.store.transaction() as tx:
                    current = tx.execute(
                        "SELECT COALESCE(MAX(n), 0) AS top FROM txn_probe"
                    ).fetchone()
                    if wait_inside:
                        first_in_tx.set()
                        time.sleep(0.5)
                    tx.execute(
                        "INSERT INTO txn_probe (n) VALUES (?)",
                        (int(current["top"]) + 1,),
                    )
            except Exception as exc:  # noqa: BLE001 — surfaced via the list
                errors.append(exc)

        slow = threading.Thread(target=writer, kwargs={"wait_inside": True})
        slow.start()
        self.assertTrue(first_in_tx.wait(timeout=10))
        fast = threading.Thread(target=writer, kwargs={"wait_inside": False})
        fast.start()
        slow.join(timeout=30)
        fast.join(timeout=30)
        self.assertEqual(errors, [])
        conn = self.store.connect()
        try:
            rows = conn.execute("SELECT n FROM txn_probe ORDER BY n").fetchall()
            self.assertEqual([int(r["n"]) for r in rows], [1, 2])
        finally:
            conn.close()

    def test_build_state_store_selects_the_postgres_dialect(self) -> None:
        store = build_state_store(
            db_path=Path("/nonexistent/unused.sqlite"),
            env={"RESEARCH_PLUGIN_DB_URL": _reset_database()},
        )
        self.assertIsInstance(store, PostgresStateStore)


def _columns_by_table(schema_sql: str) -> dict[str, set[str]]:
    """Table → column-name set, parsed from CREATE TABLE statements.

    A deliberately dumb string-level parse (the parity contract is string
    level too): the first token of each body line that is not a constraint
    or a comment is a column name.
    """
    tables: dict[str, set[str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\s*\);", schema_sql, re.DOTALL
    ):
        name, body = match.group(1), match.group(2)
        columns: set[str] = set()
        for line in body.splitlines():
            token = line.strip().split(" ", 1)[0].rstrip(",")
            if not token or token == "--":
                continue
            if token.upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}:
                continue
            columns.add(token)
        tables[name] = columns
    return tables


def _sqlite_schema_tables() -> set[str]:
    return set(_columns_by_table(SCHEMA))


class SchemaParityTest(unittest.TestCase):
    """No docker needed: the translated DDL covers the SQLite SCHEMA exactly."""

    def test_translated_ddl_has_every_sqlite_table_and_column(self) -> None:
        sqlite_tables = _columns_by_table(SCHEMA)
        postgres_tables = _columns_by_table(translate_schema_to_postgres(SCHEMA))
        self.assertTrue(sqlite_tables)  # the parse saw the schema at all
        self.assertEqual(set(postgres_tables), set(sqlite_tables))
        for table, columns in sqlite_tables.items():
            self.assertEqual(postgres_tables[table], columns, f"table {table}")

    def test_translation_strips_every_sqlite_ism(self) -> None:
        translated = translate_schema_to_postgres(SCHEMA)
        self.assertNotIn("PRAGMA", translated)
        self.assertNotIn("AUTOINCREMENT", translated)
        self.assertNotRegex(translated, r"\bINTEGER\b")  # 32-bit on Postgres
        self.assertIn("BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", translated)

    def test_no_question_marks_inside_sql_string_literals(self) -> None:
        """The dialect's '?' → '%s' translation is string-level; it is only
        sound while no SQL keeps a literal '?' or '%' inside quotes. Walk
        every SQL-looking constant in backend/ and keep that invariant."""
        import ast

        from tests.paths import BACKEND_ROOT

        offenders: list[str] = []
        for path in sorted(BACKEND_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                sql = node.value
                if not re.search(
                    r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE)\b", sql.upper()
                ):
                    continue
                for literal in re.findall(r"'(?:[^']|'')*'", sql):
                    if "?" in literal or "%" in literal:
                        offenders.append(f"{path.name}:{node.lineno}: {literal!r}")
        self.assertEqual(offenders, [])

    def test_resolve_db_url_default_and_rejection(self) -> None:
        self.assertIsNone(resolve_db_url(env={}))
        self.assertEqual(
            resolve_db_url(env={"RESEARCH_PLUGIN_DB_URL": "postgres://x/y"}),
            "postgres://x/y",
        )
        with self.assertRaises(ValidationError):
            build_state_store(
                db_path=Path("/nonexistent/unused.sqlite"),
                env={"RESEARCH_PLUGIN_DB_URL": "mysql://nope"},
            )

    def test_build_state_store_defaults_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = build_state_store(
                db_path=Path(tmp) / "state.sqlite", env={}
            )
            self.assertIsInstance(store, StateStore)


if __name__ == "__main__":
    unittest.main()
