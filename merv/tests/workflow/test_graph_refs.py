from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from merv.brain.kernel.state.store import StateStore
from merv.brain.research_core.graph_refs import GraphRefResolver


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, query, arguments):
        self.calls.append((query, arguments))
        _project_id, *refs = arguments
        rows = []
        if " FROM claims " in query and "claim_1" in refs:
            rows.append(
                {"id": "claim_1", "statement": "Claim", "status": "active"}
            )
        if " FROM experiments " in query and "exp_1" in refs:
            rows.append({"id": "exp_1", "intent": "Test", "status": "running"})
        return _Cursor(rows)

    def close(self):
        return None


class _Cursor:
    def __init__(self, rows) -> None:
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Store:
    def __init__(self) -> None:
        self.connection = _Connection()

    def connect(self):
        return self.connection


class GraphRefResolverTest(unittest.TestCase):
    def test_resolves_only_research_owned_prefixes(self) -> None:
        store = _Store()

        result = GraphRefResolver(store=store).resolve_index(
            project_id="proj_1",
            refs=(
                "claim_1",
                "results.json",
                "claim_missing",
                "res_missing",
                "exp_1",
            ),
        )

        self.assertEqual(
            result,
            {
                "claim_1": {
                    "type": "claim",
                    "resolved": True,
                    "claim_id": "claim_1",
                    "statement": "Claim",
                    "status": "active",
                },
                "claim_missing": {"type": "unknown", "resolved": False},
                "exp_1": {
                    "type": "experiment",
                    "resolved": True,
                    "experiment_id": "exp_1",
                    "intent": "Test",
                    "status": "running",
                },
            },
        )
        self.assertEqual(
            [arguments for _query, arguments in store.connection.calls],
            [
                ("proj_1", "claim_1", "claim_missing"),
                ("proj_1", "exp_1"),
            ],
        )


class CountingStateStore(StateStore):
    def __init__(self, *, db_path: Path) -> None:
        self.statements: list[str] = []
        super().__init__(db_path=db_path)

    def connect(self):
        conn = super().connect()
        conn.set_trace_callback(self.statements.append)
        return conn


class GraphRefQueryCountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CountingStateStore(
            db_path=Path(self.tmp.name) / "state.sqlite"
        )
        self.resolver = GraphRefResolver(store=self.store)
        self._seed()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        now = "2026-07-21T00:00:00Z"
        with self.store.transaction() as conn:
            for project_id in ("proj_main", "proj_other"):
                conn.execute(
                    "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                    (project_id, project_id, now),
                )
            conn.execute(
                """
                INSERT INTO review_requests (
                  id, project_id, target_type, target_id, role,
                  capability_hash, status, target_snapshot_id, expires_at,
                  created_at
                ) VALUES ('rr_graph', 'proj_main', 'experiment', 'exp_00',
                          'reviewer', 'cap_graph', 'completed', 'snap_graph',
                          ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO review_sessions (
                  id, request_id, independence, status, created_at
                ) VALUES ('rs_graph', 'rr_graph', 'independent', 'submitted', ?)
                """,
                (now,),
            )
            for index in range(401):
                conn.execute(
                    """
                    INSERT INTO claims (
                      id, project_id, statement, status, created_at
                    ) VALUES (?, 'proj_main', ?, 'active', ?)
                    """,
                    (f"claim_{index:03d}", f"Claim {index}", now),
                )
                if index >= 25:
                    continue
                conn.execute(
                    """
                    INSERT INTO experiments (
                      id, project_id, name, intent, status, created_at, updated_at
                    ) VALUES (?, 'proj_main', ?, ?, 'running', ?, ?)
                    """,
                    (f"exp_{index:02d}", f"Experiment {index}", f"Intent {index}", now, now),
                )
                conn.execute(
                    """
                    INSERT INTO reviews (
                      id, project_id, request_id, session_id, target_snapshot_id,
                      target_type, target_id, role, verdict, created_at
                    ) VALUES (?, 'proj_main', 'rr_graph', 'rs_graph',
                              'snap_graph', 'experiment', 'exp_00', 'reviewer',
                              'pass', ?)
                    """,
                    (f"rev_{index:02d}", now),
                )
                conn.execute(
                    """
                    INSERT INTO reflections (
                      id, project_id, title, status, created_at, updated_at
                    ) VALUES (?, 'proj_main', ?, 'published', ?, ?)
                    """,
                    (f"syn_{index:02d}", f"Reflection {index}", now, now),
                )
            conn.execute(
                """
                INSERT INTO claims (id, project_id, statement, status, created_at)
                VALUES ('claim_foreign', 'proj_other', 'Foreign', 'active', ?)
                """,
                (now,),
            )

    def _resolve(self, refs: tuple[str, ...]):
        self.store.statements.clear()
        result = self.resolver.resolve_index(project_id="proj_main", refs=refs)
        selects = [
            statement
            for statement in self.store.statements
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        return result, selects

    def test_one_and_twenty_five_refs_each_use_one_query_per_type(self) -> None:
        for prefix in ("claim", "exp", "rev", "syn"):
            width = 3 if prefix == "claim" else 2
            for count in (1, 25):
                with self.subTest(prefix=prefix, count=count):
                    refs = tuple(f"{prefix}_{index:0{width}d}" for index in range(count))
                    result, selects = self._resolve(refs)
                    self.assertEqual(list(result), list(refs))
                    self.assertEqual(len(selects), 1)

    def test_mixed_missing_duplicate_scope_and_chunk_semantics(self) -> None:
        refs = (
            "exp_00",
            "claim_000",
            "rev_00",
            "claim_missing",
            "syn_00",
            "claim_000",
            "claim_foreign",
            "results.json",
        )
        result, selects = self._resolve(refs)

        self.assertEqual(
            result,
            {
                "exp_00": {
                    "type": "experiment",
                    "resolved": True,
                    "experiment_id": "exp_00",
                    "intent": "Intent 0",
                    "status": "running",
                },
                "claim_000": {
                    "type": "claim",
                    "resolved": True,
                    "claim_id": "claim_000",
                    "statement": "Claim 0",
                    "status": "active",
                },
                "rev_00": {
                    "type": "review",
                    "resolved": True,
                    "review_id": "rev_00",
                    "role": "reviewer",
                    "verdict": "pass",
                    "created_at": "2026-07-21T00:00:00Z",
                },
                "claim_missing": {"type": "unknown", "resolved": False},
                "syn_00": {
                    "type": "reflection",
                    "resolved": True,
                    "reflection_id": "syn_00",
                    "title": "Reflection 0",
                    "status": "published",
                    "published_at": None,
                },
                "claim_foreign": {"type": "unknown", "resolved": False},
            },
        )
        self.assertEqual(len(selects), 4)

        chunked, chunk_selects = self._resolve(
            tuple(f"claim_{index:03d}" for index in range(401))
        )
        self.assertEqual(len(chunked), 401)
        self.assertEqual(len(chunk_selects), 2)


if __name__ == "__main__":
    unittest.main()
