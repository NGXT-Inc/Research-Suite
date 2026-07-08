"""Observability: structured logs + per-tenant counters (cloud plan Phase 9).

The structured logger emits redacted JSON to stdout in control mode only (so no
token/capability leaks and local mode stays silent); the per-tenant counters
read RED-ish usage from the events table + the generation ledger.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.execution.backends.fake import FakeSandboxBackend
from backend.observability import StructuredLogger, TenantCounters


class StructuredLoggerTest(unittest.TestCase):
    def test_disabled_logger_emits_nothing(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger(enabled=False, stream=stream)
        logger.log(kind="http", path="/api/projects", status=200)
        self.assertEqual(stream.getvalue(), "")

    def test_enabled_logger_emits_one_json_line(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger(enabled=True, stream=stream)
        logger.log(
            kind="http",
            request_id="abc123",
            tenant_id="acme",
            path="/api/projects",
            status=200,
            duration_ms=12,
            method="GET",
        )
        lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["request_id"], "abc123")
        self.assertEqual(record["tenant_id"], "acme")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["duration_ms"], 12)

    def test_sensitive_fields_are_redacted(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger(enabled=True, stream=stream)
        logger.log(
            kind="tool",
            tool="review.start",
            status="ok",
            capability="super-secret-token",
            reviewer_capability="another-secret",
        )
        out = stream.getvalue()
        self.assertNotIn("super-secret-token", out)
        self.assertNotIn("another-secret", out)
        record = json.loads(out.splitlines()[0])
        self.assertEqual(record["capability"], "[redacted]")
        self.assertEqual(record["reviewer_capability"], "[redacted]")

    def test_status_zero_is_kept_even_when_falsey(self) -> None:
        # status is always present (the empty-field prune exempts it).
        stream = io.StringIO()
        logger = StructuredLogger(enabled=True, stream=stream)
        logger.log(kind="tool", tool="x", status="")
        record = json.loads(stream.getvalue().splitlines()[0])
        self.assertIn("status", record)


class TenantCountersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.store = self.app.store
        self.counters = TenantCounters(store=self.store)
        self.project_id = self.app.call_tool("project", {"action": "create", "name": "Proj P"})["id"]
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE projects SET tenant_id = ? WHERE id = ?",
                ("tenant_x", self.project_id),
            )

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def test_counts_are_tenant_scoped(self) -> None:
        # Seed two closed generations (3h total) + a couple of events.
        with self.store.transaction() as conn:
            for i, (start, end) in enumerate(
                [
                    ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
                    ("2026-01-01T02:00:00Z", "2026-01-01T04:00:00Z"),
                ]
            ):
                conn.execute(
                    """
                    INSERT INTO sandbox_generations
                      (id, experiment_id, project_id, tenant_id, price_usd_per_hour,
                       started_at, ended_at, created_seq)
                    VALUES (?, 'exp', ?, 'tenant_x', 1.0, ?, ?, ?)
                    """,
                    (f"sbg_{i}", self.project_id, start, end, i),
                )
            self.store.record_event(
                conn=conn,
                project_id=self.project_id,
                event_type="audit.test",
                target_type="experiment",
                target_id="exp",
                payload={"k": "v"},
            )
        counts = self.counters.for_tenant(tenant_id="tenant_x")
        self.assertEqual(counts["tenant_id"], "tenant_x")
        self.assertEqual(counts["sandbox_generations"], 2)
        self.assertAlmostEqual(counts["sandbox_hours"], 3.0)
        self.assertGreaterEqual(counts["tool_calls"], 1)

    def test_other_tenant_sees_nothing(self) -> None:
        counts = self.counters.for_tenant(tenant_id="tenant_none")
        self.assertEqual(counts["sandbox_generations"], 0)
        self.assertEqual(counts["tool_calls"], 0)
        self.assertEqual(counts["sandbox_hours"], 0.0)


if __name__ == "__main__":
    unittest.main()
