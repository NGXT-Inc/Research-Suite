"""ETag/304 semantics and the SSE event stream (UI push, blind-poll relief)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from backend.execution.backends.fake import FakeSandboxBackend
from backend.transport.http_api import create_fastapi_app


class ConditionalRequestTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(create_fastapi_app(self.app))
        project = self.client.post("/api/projects", json={"name": "Cond", "summary": ""}).json()
        self.project_id = project["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def create_claim(self, statement: str) -> None:
        response = self.client.post(
            f"/api/projects/{self.project_id}/claims", json={"statement": statement}
        )
        self.assertEqual(response.status_code, 201, response.text)


class EtagTest(ConditionalRequestTestBase):
    PATHS = ["/home", "/events?limit=100", "/sandboxes"]

    def get(self, suffix: str, etag: str | None = None):
        headers = {"If-None-Match": etag} if etag else {}
        return self.client.get(f"/api/projects/{self.project_id}{suffix}", headers=headers)

    def test_etag_present_and_stable_across_identical_reads(self) -> None:
        for suffix in self.PATHS:
            first = self.get(suffix)
            second = self.get(suffix)
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.headers.get("etag"))
            self.assertEqual(first.headers["etag"], second.headers["etag"], suffix)
            # Unconditional 200 bodies stay byte-identical between reads.
            self.assertEqual(first.content, second.content, suffix)
            self.assertTrue(first.headers["content-type"].startswith("application/json"))

    def test_if_none_match_returns_304_with_empty_body(self) -> None:
        for suffix in self.PATHS:
            etag = self.get(suffix).headers["etag"]
            conditional = self.get(suffix, etag=etag)
            self.assertEqual(conditional.status_code, 304, suffix)
            self.assertEqual(conditional.content, b"", suffix)
            self.assertEqual(conditional.headers.get("etag"), etag, suffix)

    def test_multi_value_if_none_match_matches(self) -> None:
        etag = self.get("/home").headers["etag"]
        response = self.get("/home", etag=f'"stale", {etag}')
        self.assertEqual(response.status_code, 304)

    def test_mutation_rotates_etag_and_serves_fresh_200(self) -> None:
        home_etag = self.get("/home").headers["etag"]
        events_etag = self.get("/events?limit=100").headers["etag"]
        self.create_claim("Conditional gets do not go stale.")
        for suffix, etag in [("/home", home_etag), ("/events?limit=100", events_etag)]:
            response = self.get(suffix, etag=etag)
            self.assertEqual(response.status_code, 200, suffix)
            self.assertNotEqual(response.headers["etag"], etag, suffix)
        self.assertEqual(
            self.get("/home").json()["stats"]["claims"], 1
        )

    def test_stale_etag_still_serves_full_payload(self) -> None:
        response = self.get("/home", etag='"not-the-etag"')
        self.assertEqual(response.status_code, 200)
        self.assertIn("project", response.json())

    def test_events_signal_etag_short_circuits_without_rendering_body(self) -> None:
        etag = self.get("/events?limit=100").headers["etag"]
        original = self.app.store.recent_events

        def fail_recent_events(*, project_id: str | None, limit: int = 100):
            self.fail("matching event ETag should not render the events body")

        self.app.store.recent_events = fail_recent_events  # type: ignore[method-assign]
        try:
            response = self.get("/events?limit=100", etag=etag)
        finally:
            self.app.store.recent_events = original  # type: ignore[method-assign]
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")

    def test_sandboxes_signal_etag_short_circuits_without_rendering_body(self) -> None:
        # The signal reads the sandbox rows directly (project_sandbox_signal);
        # the row-VIEW render is what a matching ETag must skip.
        etag = self.get("/sandboxes").headers["etag"]
        original = self.app.sandboxes.rows

        def fail_rows(*, project_id: str | None = None):
            self.fail("matching sandbox ETag should not render the list body")

        self.app.sandboxes.rows = fail_rows  # type: ignore[method-assign]
        try:
            response = self.get("/sandboxes", etag=etag)
        finally:
            self.app.sandboxes.rows = original  # type: ignore[method-assign]
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")

    def test_home_signal_etag_short_circuits_without_rendering_body(self) -> None:
        # The composite signal never calls status_and_next; the heavy home
        # render (which leads with it) must not run on a matching ETag.
        etag = self.get("/home").headers["etag"]
        original = self.app.workflow.status_and_next

        def fail_status(*args, **kwargs):
            self.fail("matching home ETag should not render the status body")

        self.app.workflow.status_and_next = fail_status  # type: ignore[method-assign]
        try:
            response = self.get("/home", etag=etag)
        finally:
            self.app.workflow.status_and_next = original  # type: ignore[method-assign]
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")


class EventStreamTest(ConditionalRequestTestBase):
    def stream_path(self, **params: object) -> str:
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return f"/api/projects/{self.project_id}/events/stream?{query}"

    def read_session(self, path: str, headers: dict | None = None) -> list[str]:
        # TestClient buffers whole responses, so run a bounded stream session
        # (max_ms) and parse the complete SSE transcript it returns.
        response = self.client.get(f"{path}&poll_ms=100&max_ms=100", headers=headers or {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        return response.text.splitlines()

    @staticmethod
    def events_of(lines: list[str], name: str) -> list[dict]:
        rows = []
        for index, line in enumerate(lines):
            if line == f"event: {name}":
                rows.append(json.loads(lines[index + 1].removeprefix("data: ")))
        return rows

    def test_since_zero_replays_rows_then_signals_state(self) -> None:
        self.create_claim("First claim.")
        self.create_claim("Second claim.")
        recorded = self.client.get(f"/api/projects/{self.project_id}/events?limit=500").json()["events"]
        lines = self.read_session(self.stream_path(since=0))

        self.assertIn("retry: 3000", lines)
        hello = self.events_of(lines, "hello")
        self.assertEqual(hello, [{"cursor": 0}])
        appended = self.events_of(lines, "append")
        self.assertEqual(len(appended), len(recorded))
        # Ascending replay of the same rows /events returns (which is DESC).
        self.assertEqual(
            [row["id"] for row in appended],
            sorted(row["id"] for row in recorded),
        )
        # Every append carries an SSE id so EventSource reconnects can resume.
        self.assertIn(f"id: {appended[-1]['id']}", lines)
        state = self.events_of(lines, "state")
        self.assertEqual(state, [{"version": appended[-1]["id"]}])

    def test_last_event_id_header_resumes_after_cursor(self) -> None:
        self.create_claim("Old claim.")
        old_id = self.client.get(f"/api/projects/{self.project_id}/events?limit=1").json()["events"][0]["id"]
        self.create_claim("New claim.")
        lines = self.read_session(
            self.stream_path(), headers={"Last-Event-ID": str(old_id)}
        )
        appended = self.events_of(lines, "append")
        self.assertTrue(appended)
        self.assertTrue(all(row["id"] > old_id for row in appended))

    def test_stream_is_project_scoped(self) -> None:
        other = self.client.post("/api/projects", json={"name": "Other", "summary": ""}).json()
        self.client.post(f"/api/projects/{other['id']}/claims", json={"statement": "Foreign claim."})
        self.create_claim("Local claim.")
        lines = self.read_session(self.stream_path(since=0))
        appended = self.events_of(lines, "append")
        self.assertTrue(appended)
        self.assertTrue(all(row["project_id"] == self.project_id for row in appended))

    def test_unknown_project_is_a_plain_404(self) -> None:
        response = self.client.get("/api/projects/nope/events/stream?since=0")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
