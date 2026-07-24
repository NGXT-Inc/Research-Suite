"""Tests for bounded activity-log reads and result payload capping."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from merv.brain.kernel.state.activity import (
    RESULT_LOG_MAX_BYTES,
    ActivityLogger,
    cap_result,
    redact_sensitive,
    scrub_secret_text,
)
from merv.brain.surface.control.control_runtime import ControlActivitySink

# A realistic storage.submit result: bytes go direct to S3 via a presigned PUT,
# and the ledger is finalized through the one-time completion token — both live
# inside the `run` command string value.
_PRESIGNED = (
    "https://bucket.s3.amazonaws.com/proj/abc?"
    "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
    "X-Amz-Credential=AKIAEXAMPLE%2F20260723%2Fus-east-1%2Fs3%2Faws4_request&"
    "X-Amz-Date=20260723T000000Z&X-Amz-Expires=3600&"
    "X-Amz-SignedHeaders=host&X-Amz-Signature=deadbeefcafef00dsignature"
)
_S3_SIG_PARAMS = ("X-Amz-Signature=", "X-Amz-Credential=", "X-Amz-Security-Token=")


class CapResultTest(unittest.TestCase):
    def test_small_result_passes_through(self) -> None:
        value = {"projects": [{"id": "proj_1"}]}
        self.assertEqual(cap_result(value=value), value)

    def test_sensitive_result_fields_are_redacted(self) -> None:
        value = {
            "reviewer_capability": "rp_secret",
            "repo_root": "/private/repo",
            "nested": {
                "capability": "rp_nested",
                "env": {"MLFLOW_TRACKING_PASSWORD": "rr_sk_agent"},
            },
            "tuple": ({"MLFLOW_TRACKING_PASSWORD": "tuple-secret"},),
        }
        self.assertEqual(
            cap_result(value=value),
            {
                "reviewer_capability": "[redacted]",
                "nested": {
                    "capability": "[redacted]",
                    "env": {"MLFLOW_TRACKING_PASSWORD": "[redacted]"},
                },
                "tuple": ({"MLFLOW_TRACKING_PASSWORD": "[redacted]"},),
            },
        )
        self.assertNotIn("repo_root", cap_result(value=value))

    def test_presigned_url_signature_scrubbed_from_result_values(self) -> None:
        # INV-12 value-level scrubbing: a presigned S3 URL is a ~1-hour
        # replayable credential; its SigV4 signature params must never reach the
        # activity log even when embedded in a string value like `run`.
        run = (
            "curl -sf -X PUT -H 'x-amz-checksum-sha256:aGVsbG8=' -T 'model.bin' "
            f"'{_PRESIGNED}' && curl -sf -X POST "
            "'http://127.0.0.1:8787/api/storage/u/tok_SECRET/complete'"
        )
        value = {"object": {"id": "sto_1"}, "run": run, "upload_id": "upload_1"}
        scrubbed = cap_result(value=value)
        serialized = json.dumps(scrubbed)
        for param in _S3_SIG_PARAMS:
            self.assertNotIn(param, serialized)
        # The signed access key id and the completion token are both gone.
        self.assertNotIn("AKIAEXAMPLE", serialized)
        self.assertNotIn("tok_SECRET", serialized)
        # The command structure survives so the log stays legible.
        self.assertIn("/api/storage/u/<redacted>/complete", scrubbed["run"])
        self.assertIn("x-amz-checksum-sha256:aGVsbG8=", scrubbed["run"])
        self.assertEqual(scrubbed["object"], {"id": "sto_1"})

    def test_scrub_secret_text_is_precise(self) -> None:
        # The SigV4 params are dropped entirely; the URL host/key survives, and
        # non-token /api paths pass through untouched.
        cleaned = scrub_secret_text(_PRESIGNED)
        self.assertNotIn("X-Amz-Signature=", cleaned)
        self.assertNotIn("X-Amz-Credential=", cleaned)
        self.assertNotIn("AKIAEXAMPLE", cleaned)
        self.assertIn("<redacted>", cleaned)
        self.assertIn("bucket.s3.amazonaws.com/proj/abc", cleaned)
        self.assertEqual(
            scrub_secret_text("/api/artifacts/u/tok_x"), "/api/artifacts/u/<redacted>"
        )
        # feed.post returns its one-time upload token inside `run`; the value
        # scrubber must cover /api/feed/u the same as the HTTP-path scrubber, or
        # the bearer token persists unredacted in tool telemetry (INV-12).
        self.assertEqual(
            scrub_secret_text("/api/feed/u/tok_feed"), "/api/feed/u/<redacted>"
        )
        self.assertEqual(
            scrub_secret_text("/api/projects/p_1/storage"), "/api/projects/p_1/storage"
        )
        # A plain string with no secrets is returned unchanged (fast path).
        self.assertEqual(scrub_secret_text("nothing to see"), "nothing to see")
        self.assertIsInstance(redact_sensitive(value="nothing to see"), str)

    def test_truncated_preview_does_not_embed_local_fields(self) -> None:
        value = {
            "repo_root": "/private/repo",
            "local_sync_dir": "/private/sync",
            "blob": "x" * (RESULT_LOG_MAX_BYTES + 1000),
        }
        capped = cap_result(value=value)
        self.assertTrue(capped["_truncated"])
        self.assertNotIn("repo_root", capped["preview"])
        self.assertNotIn("/private/repo", capped["preview"])
        self.assertNotIn("local_sync_dir", capped["preview"])
        self.assertNotIn("/private/sync", capped["preview"])

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

    def test_control_sink_reuses_the_canonical_tool_event_methods(self) -> None:
        self.assertIs(ControlActivitySink.tool_ok, ActivityLogger.tool_ok)
        self.assertIs(ControlActivitySink.tool_error, ActivityLogger.tool_error)

    def test_recent_returns_most_recent_within_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            logger = self._logger(tmp)
            for i in range(50):
                logger.emit(event_type="tool.call", payload={"i": i, "source": "http"})
            recent = logger.recent(limit=5)
            got = [event["i"] for event in recent["events"]]
            self.assertEqual(got, [45, 46, 47, 48, 49])

    def test_event_filter_applies_before_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            logger = self._logger(tmp)
            logger.tool_ok(
                source="mcp",
                tool="claim.list",
                arguments={"project_id": "p1"},
                duration_ms=1,
                result={"claims": []},
            )
            logger.tool_ok(
                source="mcp",
                tool="claim.list",
                arguments={"project_id": "p2"},
                duration_ms=1,
                result={"claims": []},
            )

            recent = logger.recent(
                limit=1,
                source="mcp",
                event_filter=lambda event: event.get("args", {}).get("project_id") == "p1",
            )

            self.assertEqual(len(recent["events"]), 1)
            self.assertEqual(recent["events"][0]["args"]["project_id"], "p1")

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

    def test_tool_ok_records_true_io_sizes_even_when_capped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            logger = self._logger(tmp)
            big = "z" * (RESULT_LOG_MAX_BYTES + 5000)
            logger.tool_ok(
                source="mcp",
                tool="experiment.get_state",
                arguments={"experiment_id": "exp_1"},
                duration_ms=12,
                result={"blob": big},
            )
            event = json.loads((tmp / "activity.jsonl").read_text().splitlines()[-1])
            # The on-disk result is truncated, but the recorded size reflects the
            # FULL payload the agent actually received.
            self.assertTrue(event["result"]["_truncated"])
            self.assertGreater(event["received_chars"], RESULT_LOG_MAX_BYTES)
            self.assertGreater(event["sent_chars"], 0)

    def test_tool_error_records_sent_and_error_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            logger = self._logger(tmp)
            logger.tool_error(
                source="mcp",
                tool="sandbox.request",
                arguments={"experiment_id": "exp_1"},
                duration_ms=4,
                error="boom",
                error_code="bad",
            )
            event = json.loads((tmp / "activity.jsonl").read_text().splitlines()[-1])
            self.assertEqual(event["received_chars"], len("boom"))
            self.assertGreater(event["sent_chars"], 0)


if __name__ == "__main__":
    unittest.main()
