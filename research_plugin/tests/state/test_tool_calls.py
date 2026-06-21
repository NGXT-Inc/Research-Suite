"""Tests for the full-fidelity tool-call store backing the debug analyzer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.control_runtime import ControlToolCallSink
from backend.domain.tool_call_stats import percentile
from backend.state.tool_calls import ToolCallStore


class PercentileTest(unittest.TestCase):
    def test_inclusive_quantile(self) -> None:
        self.assertEqual(percentile([], 95), 0)
        self.assertEqual(percentile([42], 95), 42)
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2)
        self.assertEqual(percentile(list(range(1, 101)), 95), 95)


class ToolCallStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ToolCallStore(db_path=Path(self.tmp.name) / "tc.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        self.store.record(
            tool="experiment.get_state", source="mcp", status="ok", duration_ms=30,
            arguments={"project_id": "p", "experiment_id": "e1"},
            result={"experiment": {"id": "e1", "blob": "x" * 5000}},
        )
        self.store.record(
            tool="experiment.get_state", source="mcp", status="ok", duration_ms=10,
            arguments={"project_id": "p", "experiment_id": "e2"},
            result={"experiment": {"id": "e2"}},
        )
        self.store.record(
            tool="claim.list", source="http", status="ok", duration_ms=2,
            arguments={"project_id": "p"}, result={"claims": []},
        )
        self.store.record(
            tool="sandbox.request", source="mcp", status="error", duration_ms=4,
            arguments={"project_id": "p", "experiment_id": "e"},
            error="modal unavailable", error_code="unavailable",
        )

    def test_stats_aggregate_and_percentiles(self) -> None:
        self._seed()
        stats = self.store.stats(limit=50)
        self.assertEqual(stats["totals"]["calls"], 4)
        self.assertEqual(stats["totals"]["error_calls"], 1)
        top = stats["by_tool"][0]
        self.assertEqual(top["tool"], "experiment.get_state")
        self.assertEqual(top["calls"], 2)
        self.assertGreaterEqual(top["max_received_chars"], 5000)
        for field in ("avg_received_chars", "p50_received_chars", "p95_received_chars"):
            self.assertIn(field, top)

    def test_get_returns_full_native_payload(self) -> None:
        self._seed()
        stats = self.store.stats()
        call = next(c for c in stats["calls"] if c["tool"] == "experiment.get_state" and c["received_chars"] > 1000)
        detail = self.store.get(call_id=call["id"])
        self.assertIsNotNone(detail)
        # Result comes back as native JSON, not a string.
        self.assertIsInstance(detail["result"], dict)
        self.assertEqual(detail["result"]["experiment"]["id"], "e1")
        self.assertIsInstance(detail["args"], dict)
        self.assertFalse(detail["result_truncated"])

    def test_error_call_stores_message(self) -> None:
        self._seed()
        stats = self.store.stats(status="error")
        self.assertEqual(stats["totals"]["calls"], 1)
        detail = self.store.get(call_id=stats["calls"][0]["id"])
        self.assertEqual(detail["status"], "error")
        self.assertEqual(detail["result"], "modal unavailable")
        self.assertEqual(detail["received_chars"], len("modal unavailable"))

    def test_filters(self) -> None:
        self._seed()
        self.assertEqual(self.store.stats(tool="experiment")["totals"]["calls"], 2)
        self.assertEqual(self.store.stats(source="http")["totals"]["calls"], 1)
        self.assertEqual(self.store.stats(status="error")["totals"]["calls"], 1)

    def test_project_allow_list_filters_stats_get_and_clear(self) -> None:
        self.store.record(
            tool="claim.list", source="http", status="ok", duration_ms=1,
            arguments={"project_id": "p1"}, result={"claims": []},
        )
        self.store.record(
            tool="claim.list", source="http", status="ok", duration_ms=1,
            arguments={"project_id": "p2"}, result={"claims": []},
        )

        stats = self.store.stats(project_ids={"p1"})
        self.assertEqual(stats["totals"]["calls"], 1)
        self.assertEqual(stats["calls"][0]["project_id"], "p1")
        p2_call = self.store.stats(project_id="p2")["calls"][0]
        self.assertIsNone(
            self.store.get(call_id=p2_call["id"], project_ids={"p1"})
        )

        cleared = self.store.clear(project_ids={"p1"})
        self.assertEqual(cleared["cleared"], 1)
        remaining = self.store.stats()
        self.assertEqual(remaining["totals"]["calls"], 1)
        self.assertEqual(remaining["calls"][0]["project_id"], "p2")

    def test_sensitive_keys_redacted_in_args_and_result(self) -> None:
        self.store.record(
            tool="review.request", source="mcp", status="ok", duration_ms=1,
            arguments={
                "project_id": "p",
                "reviewer_capability": "rp_arg",
                "nested": {"capability": "rp_nested"},
            },
            result={
                "reviewer_capability": "rp_result",
                "nested": {"capability": "rp_result_nested"},
            },
        )

        detail = self.store.get(call_id=self.store.stats()["calls"][0]["id"])
        self.assertEqual(detail["args"]["reviewer_capability"], "[redacted]")
        self.assertEqual(detail["args"]["nested"]["capability"], "[redacted]")
        self.assertEqual(detail["result"]["reviewer_capability"], "[redacted]")
        self.assertEqual(detail["result"]["nested"]["capability"], "[redacted]")

    def test_sort_calls(self) -> None:
        self._seed()
        desc = self.store.stats(sort="received_chars", order="desc")["calls"]
        self.assertEqual(desc[0]["received_chars"], max(c["received_chars"] for c in desc))
        asc = self.store.stats(sort="received_chars", order="asc")["calls"]
        self.assertEqual(asc[0]["received_chars"], min(c["received_chars"] for c in asc))

    def test_oversized_payload_truncated_but_size_exact(self) -> None:
        small = ToolCallStore(db_path=Path(self.tmp.name) / "tc2.sqlite", max_payload_chars=500)
        small.record(
            tool="big", source="mcp", status="ok", duration_ms=1,
            arguments={}, result={"blob": "z" * 4000},
        )
        stats = small.stats()
        self.assertGreater(stats["by_tool"][0]["received_chars"], 4000)  # true size
        detail = small.get(call_id=stats["calls"][0]["id"])
        self.assertTrue(detail["result_truncated"])
        self.assertTrue(detail["result"]["_truncated"])

    def test_row_cap_evicts_oldest(self) -> None:
        capped = ToolCallStore(db_path=Path(self.tmp.name) / "tc3.sqlite", max_rows=10)
        for i in range(25):
            capped.record(tool="t", source="mcp", status="ok", duration_ms=1, arguments={"i": i}, result={"i": i})
        stats = capped.stats(limit=100)
        self.assertLessEqual(stats["totals"]["calls"], 11)
        self.assertTrue(stats["coverage"]["capped"])

    def test_clear(self) -> None:
        self._seed()
        result = self.store.clear()
        self.assertEqual(result["cleared"], 4)
        self.assertEqual(self.store.stats()["totals"]["calls"], 0)

    def test_disabled_store_is_inert(self) -> None:
        store = ToolCallStore(db_path=Path(self.tmp.name) / "off.sqlite", enabled=False)
        store.record(tool="t", source="mcp", status="ok", duration_ms=1, arguments={}, result={})
        self.assertEqual(store.stats()["totals"]["calls"], 0)
        self.assertIsNone(store.get(call_id=1))


class ToolCallStatsParityTest(unittest.TestCase):
    def test_local_and_control_rollups_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ToolCallStore(db_path=Path(tmp) / "tc.sqlite")
            control = ControlToolCallSink()
            for sink in (store, control):
                sink.record(
                    tool="experiment.get_state",
                    source="mcp",
                    status="ok",
                    duration_ms=10,
                    arguments={"project_id": "p"},
                    result={"blob": "a"},
                )
                sink.record(
                    tool="experiment.get_state",
                    source="mcp",
                    status="ok",
                    duration_ms=30,
                    arguments={"project_id": "p"},
                    result={"blob": "b" * 20},
                )
                sink.record(
                    tool="sandbox.request",
                    source="mcp",
                    status="error",
                    duration_ms=40,
                    arguments={"project_id": "p"},
                    error="unavailable",
                    error_code="unavailable",
                )

            local_stats = store.stats(limit=50)
            control_stats = control.stats(limit=50)
            self.assertEqual(control_stats["totals"], local_stats["totals"])
            self.assertEqual(
                self._without_last_ts(control_stats["by_tool"]),
                self._without_last_ts(local_stats["by_tool"]),
            )

    def test_rollup_helpers_are_single_sourced(self) -> None:
        backend = Path(__file__).resolve().parents[2] / "backend"
        for rel_path in ("state/tool_calls.py", "control_runtime.py"):
            source = (backend / rel_path).read_text(encoding="utf-8")
            with self.subTest(module=rel_path):
                self.assertNotIn("def _percentile", source)
                self.assertNotIn("def _by_tool", source)
                self.assertNotIn("def _accumulate", source)
                self.assertNotIn("def _finalize_bucket", source)
                self.assertIn("domain.tool_call_stats", source)

    @staticmethod
    def _without_last_ts(rows: list[dict]) -> list[dict]:
        normalized = []
        for row in rows:
            copy = dict(row)
            copy.pop("last_ts", None)
            normalized.append(copy)
        return normalized


if __name__ == "__main__":
    unittest.main()
