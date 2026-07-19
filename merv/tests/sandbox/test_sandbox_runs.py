"""Brain-side merv_run observation: reconciler, events, tool, and nudge.

Uses the fake backend's raw-listing knob, so every test exercises the real
wire parser plus the ledger — the same path a Lambda/Modal listing takes.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend


def listing(*runs: dict) -> str:
    """Raw on-box listing text, exactly as runs_listing_command emits it."""
    blocks = []
    for run in runs:
        meta = json.dumps(
            {
                "label": run["label"],
                "command": run.get("command", "python train.py"),
                "pid": run.get("pid", 4242),
                "started_at": run.get("started_at", "2026-07-05T10:00:00Z"),
            }
        )
        exit_code = run.get("exit_code")
        blocks.append(
            f"===MERV_RUN {run['label']}\n{meta}\n"
            f"===EXIT {'' if exit_code is None else exit_code}\n"
            f"===FIN {run.get('finished_at', '')}\n"
        )
    return "".join(blocks)


class SandboxRunsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.fake = FakeSandboxBackend()
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.fake,
        )
        self.project_id = self.app.call_tool("project", {"action": "create", "name": "Runs"})["id"]
        self.experiment_id = self._experiment("long-training")
        view = self.app.call_tool(
            "sandbox.request",
            {"project_id": self.project_id, "experiment_id": self.experiment_id},
        )
        self.sandbox_uid = view["sandbox_uid"]
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=self.sandbox_uid)
        self.sandbox_id = str(row["sandbox_id"])

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _experiment(self, name: str) -> str:
        exp_id = self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": name, "intent": "x"},
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?",
                (exp_id,),
            )
        return exp_id

    def _run_finished_events(self) -> list[dict]:
        conn = self.app.store.connect()
        try:
            rows = conn.execute(
                "SELECT payload_json FROM events WHERE type = 'run.finished'"
            ).fetchall()
            return [json.loads(str(row["payload_json"])) for row in rows]
        finally:
            conn.close()

    def _runs(self, arguments: dict | None = None) -> dict:
        args = {"project_id": self.project_id, "experiment_id": self.experiment_id}
        args.update(arguments or {})
        return self.app.call_tool("sandbox.runs", args)

    # ---------- reconciler + tool shape ----------

    def test_runs_tool_reports_running_and_finished(self) -> None:
        self.fake.run_listings[self.sandbox_id] = listing(
            {"label": "seed0"},
            {"label": "prep", "exit_code": 0, "finished_at": "2026-07-05T10:05:00Z"},
        )
        view = self._runs()
        self.assertEqual(view["live"], 1)
        self.assertEqual(view["finished"], 1)
        runs = {run["label"]: run for run in view["runs"]}
        self.assertEqual(runs["seed0"]["status"], "running")
        self.assertNotIn("exit_code", runs["seed0"])
        self.assertEqual(runs["prep"]["status"], "finished")
        self.assertEqual(runs["prep"]["exit_code"], 0)
        self.assertEqual(runs["prep"]["finished_at"], "2026-07-05T10:05:00Z")
        self.assertEqual(runs["prep"]["log"], ".runs/prep/log.txt")
        self.assertEqual(runs["seed0"]["started_at"], "2026-07-05T10:00:00Z")
        # Compactness: the whole response for two runs stays small.
        self.assertLess(len(json.dumps(view)), 500)

    def test_empty_and_unlaunched_sandbox_answers_with_guidance(self) -> None:
        view = self._runs()
        self.assertEqual(view["runs"], [])
        self.assertIn("merv_run", view["hint"])

    def test_run_finished_event_is_emitted_exactly_once(self) -> None:
        self.fake.run_listings[self.sandbox_id] = listing({"label": "seed0"})
        self.app.sandboxes.runs_ledger.reconcile_live()
        self.assertEqual(self._run_finished_events(), [])
        self.fake.run_listings[self.sandbox_id] = listing(
            {"label": "seed0", "exit_code": 1, "finished_at": "2026-07-05T11:00:00Z"}
        )
        # Reconcile repeatedly — daemon sweeps, tool reads, restarts.
        self.app.sandboxes.runs_ledger.reconcile_live()
        self.app.sandboxes.runs_ledger.reconcile_live()
        self._runs()
        events = self._run_finished_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["label"], "seed0")
        self.assertEqual(events[0]["exit_code"], 1)
        self.assertEqual(events[0]["sandbox_uid"], self.sandbox_uid)

    def test_crashed_box_marks_unfinished_runs_lost_without_event(self) -> None:
        self.fake.run_listings[self.sandbox_id] = listing({"label": "seed0"})
        self._runs()
        self.fake.kill(sandbox_id=self.sandbox_id)
        # The liveness reconcile (sandbox.get path) drives the row terminal;
        # a dead box answers nothing, so the mirror keeps the last records.
        self.app.call_tool(
            "sandbox.get",
            {"project_id": self.project_id, "experiment_id": self.experiment_id},
        )
        view = self._runs()
        self.assertEqual(view["runs"][0]["status"], "lost")
        self.assertEqual(view["lost"], 1)
        self.assertEqual(self._run_finished_events(), [])

    def test_receipts_outlive_the_sandbox(self) -> None:
        self.fake.run_listings[self.sandbox_id] = listing(
            {"label": "sweep", "exit_code": 0, "finished_at": "2026-07-05T12:00:00Z"}
        )
        self._runs()
        self.app.call_tool(
            "sandbox.release",
            {
                "project_id": self.project_id,
                "experiment_id": self.experiment_id,
                "confirm_retained": True,
            },
        )
        view = self._runs()
        self.assertEqual(view["finished"], 1)
        self.assertEqual(view["runs"][0]["label"], "sweep")
        self.assertEqual(len(self._run_finished_events()), 1)

    # ---------- long-poll ----------

    def test_wait_returns_early_when_a_run_finishes(self) -> None:
        self.fake.run_listings[self.sandbox_id] = listing({"label": "train"})
        self.app.sandboxes.runs_wait_poll_seconds = 0.05

        def finish_soon() -> None:
            time.sleep(0.3)
            self.fake.run_listings[self.sandbox_id] = listing(
                {"label": "train", "exit_code": 0, "finished_at": "2026-07-05T13:00:00Z"}
            )

        threading.Thread(target=finish_soon, daemon=True).start()
        started = time.monotonic()
        view = self.app.sandboxes.runs(
            experiment_id=self.experiment_id,
            project_id=self.project_id,
            wait_seconds=30,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0, "long-poll did not return early")
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertEqual(view["finished"], 1)
        self.assertEqual(view["runs"][0]["exit_code"], 0)

    def test_wait_returns_immediately_when_nothing_is_running(self) -> None:
        self.fake.run_listings[self.sandbox_id] = listing(
            {"label": "done", "exit_code": 0, "finished_at": "2026-07-05T13:00:00Z"}
        )
        started = time.monotonic()
        view = self._runs({"wait_seconds": 20})
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(view["finished"], 1)

    # ---------- nudge ----------

    def test_sandbox_tools_carry_the_nudge_iff_runs_exist(self) -> None:
        get_args = {"project_id": self.project_id, "experiment_id": self.experiment_id}
        self.assertNotIn("runs", self.app.call_tool("sandbox.get", get_args))
        self.assertNotIn("runs", self.app.call_tool("sandbox.terminal", get_args))
        self.fake.run_listings[self.sandbox_id] = listing(
            {"label": "seed0"},
            {"label": "prep", "exit_code": 0, "finished_at": "2026-07-05T10:05:00Z"},
        )
        self.app.sandboxes.runs_ledger.reconcile_live()
        nudge = self.app.call_tool("sandbox.get", get_args)["runs"]
        self.assertIn("1 live", nudge)
        self.assertIn("seed0", nudge)
        self.assertIn("1 finished (prep, exit 0)", nudge)
        self.assertIn("sandbox.runs for detail", nudge)
        self.assertIn("runs", self.app.call_tool("sandbox.terminal", get_args))
        release = self.app.call_tool("sandbox.release", get_args)
        self.assertEqual(release["status"], "confirmation_required")
        self.assertIn("runs", release)
        # The detail tool itself never carries the nudge line.
        self.assertNotIsInstance(self._runs().get("runs"), str)


if __name__ == "__main__":
    unittest.main()
