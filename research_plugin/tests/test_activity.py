"""Tests for bounded activity-log reads and result payload capping."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.state.activity import (
    RESULT_LOG_MAX_BYTES,
    ActivityLogger,
    cap_result,
)


class CapResultTest(unittest.TestCase):
    def test_small_result_passes_through(self) -> None:
        value = {"projects": [{"id": "proj_1"}]}
        self.assertEqual(cap_result(value=value), value)

    def test_oversized_result_is_truncated(self) -> None:
        value = {"blob": "x" * (RESULT_LOG_MAX_BYTES + 1000)}
        capped = cap_result(value=value)
        self.assertTrue(capped["_truncated"])
        self.assertGreater(capped["_bytes"], RESULT_LOG_MAX_BYTES)
        self.assertLessEqual(len(capped["preview"]), 2048)
        # The capped marker itself stays small.
        self.assertLessEqual(
            len(json.dumps(capped)), RESULT_LOG_MAX_BYTES // 2
        )


class TailReadTest(unittest.TestCase):
    def _logger(self, tmp: Path) -> ActivityLogger:
        return ActivityLogger(
            repo_root=tmp,
            log_path=tmp / "activity.jsonl",
            enabled=True,
            mirror_stderr=False,
        )

    def test_recent_returns_most_recent_within_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            logger = self._logger(tmp)
            for i in range(50):
                logger.emit(event_type="tool.call", payload={"i": i, "source": "http"})
            recent = logger.recent(limit=5)
            got = [event["i"] for event in recent["events"]]
            self.assertEqual(got, [45, 46, 47, 48, 49])

    def test_recent_is_bounded_by_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            logger = self._logger(tmp)
            # Each line is ~2KB. Write enough to exceed a tiny byte budget so the
            # tail reader must drop the oldest lines and the partial first line.
            big = "y" * 2000
            for i in range(200):
                logger.emit(event_type="tool.call", payload={"i": i, "pad": big})
            # 8KB budget holds only the last ~3-4 full lines.
            lines = logger._tail_lines(max_lines=1000, max_bytes=8 * 1024)
            parsed = [json.loads(line) for line in lines]
            self.assertTrue(parsed, "tail read returned no lines")
            # Every returned line parses cleanly (no partial first line).
            self.assertEqual(parsed[-1]["i"], 199)
            # Bounded: far fewer than the 200 written.
            self.assertLess(len(parsed), 20)

    def test_tool_ok_caps_large_result_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            logger = self._logger(tmp)
            logger.tool_ok(
                source="http",
                tool="experiment.get_state",
                arguments={"experiment_id": "exp_1"},
                duration_ms=12,
                result={"blob": "z" * (RESULT_LOG_MAX_BYTES + 5000)},
            )
            event = json.loads((tmp / "activity.jsonl").read_text().splitlines()[-1])
            self.assertTrue(event["result"]["_truncated"])


if __name__ == "__main__":
    unittest.main()
