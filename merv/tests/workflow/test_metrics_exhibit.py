"""System-authored metrics exhibit: generation, gating, and pinning.

The exhibit shifts quantitative evidence from attestation to observation —
the system generates the record from MLflow readback and pinned result files;
the agent writes interpretation around it. These tests cover the pure builder
(windowing, provenance, determinism) and the tool-level flow (finalize+pin at
submit_results, report-reference gate, preview parity, agent immutability,
and the no-run qualitative bypass).
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.mlflow.exhibit import WINDOW_SKEW_MS, build_metrics_exhibit, exhibit_bytes
from backend.mlflow.metrics import MAX_METRIC_KEYS, MAX_RUNS
from backend.mlflow.tracking import MlflowTrackingContext
from backend.utils import ValidationError, WorkflowError

WINDOW_START = "2026-07-05T10:00:00Z"
WINDOW_START_MS = 1_783_245_600_000  # 2026-07-05T10:00:00Z


def _run(
    run_id: str,
    *,
    start_ms: int,
    tags: dict[str, str] | None = None,
    accuracy: float = 0.5,
) -> dict:
    return {
        "run_id": run_id,
        "run_name": run_id,
        "status": "FINISHED",
        "start_time": start_ms,
        "end_time": start_ms + 60_000,
        "params": {"seed": run_id[-1]},
        "tags": tags or {},
        "metrics": {"accuracy": {"last": accuracy, "step": 10, "timestamp": start_ms}},
        "history": {"accuracy": [[0, 0.1], [10, accuracy]]},
    }


def _snapshot(runs: list[dict], *, name: str = "rp/proj/exp") -> dict:
    return {
        "available": True,
        "source": "mlflow",
        "experiments": [{"experiment_id": "1", "name": name, "runs": runs}],
    }


def _build(**overrides) -> dict:
    kwargs = dict(
        project_id="proj",
        experiment_id="exp",
        attempt_index=1,
        experiment_name="rp/proj/exp",
        window_started_at=WINDOW_START,
        snapshot=None,
        mlflow_configured=True,
        file_sources=[],
    )
    kwargs.update(overrides)
    return build_metrics_exhibit(**kwargs)


class ExhibitBuilderTest(unittest.TestCase):
    def test_all_attempt_window_runs_included_uncurated(self) -> None:
        # Five seeds, one good one: five rows, ordered by start time.
        runs = [
            _run(f"seed-{i}", start_ms=WINDOW_START_MS + i * 1000, accuracy=0.4 + 0.1 * (i == 3))
            for i in range(5)
        ]
        exhibit = _build(snapshot=_snapshot(runs))
        self.assertEqual([r["run_id"] for r in exhibit["runs"]], [f"seed-{i}" for i in range(5)])
        self.assertEqual(exhibit["verdict"]["runs_found"], 5)
        # Provenance on every entry.
        for entry in exhibit["runs"]:
            self.assertEqual(entry["source"]["type"], "mlflow")
            self.assertEqual(entry["source"]["run_id"], entry["run_id"])
            self.assertTrue(entry["started_at"])

    def test_runs_before_the_attempt_window_are_excluded(self) -> None:
        runs = [
            _run("previous-attempt", start_ms=WINDOW_START_MS - 3_600_000),
            _run("current", start_ms=WINDOW_START_MS + 1000),
        ]
        exhibit = _build(snapshot=_snapshot(runs))
        self.assertEqual([r["run_id"] for r in exhibit["runs"]], ["current"])
        self.assertEqual(exhibit["mlflow"]["runs_excluded_by_window"], 1)

    def test_attempt_window_tolerates_small_clock_skew(self) -> None:
        runs = [
            _run("within-skew", start_ms=WINDOW_START_MS - WINDOW_SKEW_MS + 1),
            _run("outside-skew", start_ms=WINDOW_START_MS - WINDOW_SKEW_MS - 1),
        ]
        exhibit = _build(snapshot=_snapshot(runs))
        self.assertEqual(
            [run["run_id"] for run in exhibit["runs"]], ["within-skew"]
        )
        self.assertEqual(exhibit["mlflow"]["runs_excluded_by_window"], 1)

    def test_missing_window_start_includes_all_runs(self) -> None:
        runs = [_run("early", start_ms=WINDOW_START_MS - 3_600_000)]
        exhibit = _build(snapshot=_snapshot(runs), window_started_at=None)
        self.assertEqual(exhibit["verdict"]["runs_found"], 1)

    def test_result_file_sources_carry_provenance_and_data(self) -> None:
        source = {
            "path": "experiments/exp/metrics.json",
            "version_id": "rver_1",
            "sha256": "ab" * 32,
            "observed_at": WINDOW_START,
            "data": {"accuracy": 0.72},
        }
        exhibit = _build(file_sources=[source])
        entry = exhibit["result_files"][0]
        self.assertEqual(entry["data"], {"accuracy": 0.72})
        self.assertEqual(entry["source"]["type"], "result_file")
        self.assertEqual(entry["source"]["version_id"], "rver_1")
        self.assertEqual(exhibit["verdict"]["result_files"], 1)

    def test_generation_is_deterministic_for_identical_state(self) -> None:
        kwargs = dict(
            snapshot=_snapshot([_run("seed-0", start_ms=WINDOW_START_MS + 1000)]),
            file_sources=[{"path": "metrics.json", "version_id": "v", "sha256": "x", "observed_at": WINDOW_START, "data": {"a": 1}}],
        )
        self.assertEqual(exhibit_bytes(_build(**kwargs)), exhibit_bytes(_build(**kwargs)))

    def test_unavailable_snapshot_yields_empty_visible_record(self) -> None:
        exhibit = _build(snapshot={"available": False})
        self.assertEqual(exhibit["runs"], [])
        self.assertFalse(exhibit["mlflow"]["available"])
        self.assertTrue(exhibit["mlflow"]["configured"])

    def test_full_snapshot_page_flags_the_cap(self) -> None:
        runs = [_run(f"r{i}", start_ms=WINDOW_START_MS + i) for i in range(MAX_RUNS)]
        exhibit = _build(snapshot=_snapshot(runs))
        self.assertEqual(exhibit["mlflow"]["runs_capped_at"], MAX_RUNS)

    def test_run_metric_cap_is_carried_into_exhibit(self) -> None:
        run = _run("capped", start_ms=WINDOW_START_MS + 1000)
        run["metrics_capped_at"] = MAX_METRIC_KEYS
        exhibit = _build(snapshot=_snapshot([run]))
        self.assertEqual(exhibit["runs"][0]["metrics_capped_at"], MAX_METRIC_KEYS)


class FakeMlflowTracking:
    """results_metrics-shaped double; runs are appended by the tests."""

    def __init__(self) -> None:
        self.tracking_uri = "http://mlflow.test"
        self.server_uri = "http://mlflow.test"
        self.available = True
        self.runs: list[dict] = []

    def context(self, *, project_id: str, experiment_id: str, **_: object) -> MlflowTrackingContext:
        return MlflowTrackingContext(
            configured=True,
            mode="external",
            tracking_uri=self.tracking_uri,
            dashboard_url="",
            experiment_name=f"rp/{project_id}/{experiment_id}",
            env={},
        )

    def create_run(self, **_: object) -> dict:
        return {"created": True, "configured": True, "run_id": "run-plugin", "run_name": "plugin", "status": "RUNNING"}

    def results_metrics(self, *, project_id: str, experiment_id: str) -> dict:
        if not self.available:
            return {"experiment_id": experiment_id, "available": False, "source": "mlflow"}
        return {
            "experiment_id": experiment_id,
            "available": True,
            "source": "mlflow",
            "experiments": [
                {
                    "experiment_id": "1",
                    "name": f"rp/{project_id}/{experiment_id}",
                    "runs": list(self.runs),
                }
            ],
        }


VALID_PLAN = (
    "## Summary\nToy experiment for exhibit tests.\n\n"
    "## Objective & hypothesis\nThreshold beats baseline.\n\n"
    "## Evaluation\nAccuracy vs baseline; success if > 0.6.\n"
)

VALID_GRAPH = (
    '{"version": 1, "nodes": ['
    '{"id": "obj", "kind": "objective", "label": "Beat baseline"},'
    '{"id": "out", "kind": "outcome", "label": "Met"}],'
    ' "edges": [{"from": "obj", "to": "out", "label": "confirmed"}]}\n'
)

REPORT_WITH_REFERENCE = (
    "## Summary\nRan per plan.\n\n"
    "## Results\nAll runs: [metrics exhibit](metrics_exhibit.json); the good "
    "seed cleared 0.6 and the four flat seeds are discussed below.\n\n"
    "## Deviations from plan\nNone.\n\n"
    "## Conclusion\nDecision rule met.\n"
)

REPORT_WITHOUT_REFERENCE = (
    "## Summary\nRan per plan.\n\n"
    "## Results\nAccuracy was 0.72, comfortably above the 0.6 threshold.\n\n"
    "## Deviations from plan\nNone.\n\n"
    "## Conclusion\nDecision rule met.\n"
)


class ExhibitFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.mlflow = FakeMlflowTracking()
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            mlflow_tracking=self.mlflow,
        )
        self.project_id = self.call("project", action="create", name="Exhibit Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    # ---- helpers ----

    def _write_and_associate(self, *, exp_id: str, path: str, role: str, body: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        self.call(
            "resource.register",
            project_id=self.project_id,
            path=path,
            kind=role,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )

    def _pass_review(self, *, exp_id: str, role: str) -> None:
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id=f"{role}-reviewer",
        )
        self.call(
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict="pass",
            synopsis="The attempt checks out against the exhibit, so it stands.",
        )

    def _drive_to_running(self, *, name: str = "exp-1") -> str:
        exp_id = self.call(
            "experiment.create", name=name, project_id=self.project_id, intent="Exhibit flow."
        )["id"]
        self._write_and_associate(exp_id=exp_id, path="plan.md", role="plan", body=VALID_PLAN)
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_design")
        self._pass_review(exp_id=exp_id, role="design_reviewer")
        self.call("experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="mark_ready_to_run")
        started = self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="start_running"
        )
        # Expectation-setting at start: the agent is told the exhibit IS the record.
        self.assertIn("metrics_exhibit.json", started["metrics_exhibit"]["notice"])
        self.assertEqual(started["metrics_exhibit"]["preview_tool"], "experiment.exhibit")
        return exp_id

    def _log_run(self, run_id: str, *, offset_ms: int = 0) -> None:
        self.mlflow.runs.append(_run(run_id, start_ms=int(time.time() * 1000) + offset_ms))

    def _submit_ready(self, exp_id: str, *, report: str = REPORT_WITH_REFERENCE) -> None:
        self._write_and_associate(exp_id=exp_id, path="results.json", role="result", body='{"accuracy": 0.72}\n')
        self._write_and_associate(exp_id=exp_id, path="report.md", role="report", body=report)
        self._write_and_associate(exp_id=exp_id, path="graph.json", role="graph", body=VALID_GRAPH)

    def _exhibit_association(self, exp_id: str) -> dict | None:
        conn = self.app._store.connect()
        try:
            row = conn.execute(
                """
                SELECT a.resource_id, a.version_id, r.path, r.created_by,
                       v.content_sha256
                FROM resource_associations a
                JOIN resources r ON r.id = a.resource_id
                JOIN resource_versions v ON v.id = a.version_id
                WHERE a.target_type = 'experiment' AND a.target_id = ?
                  AND a.role = 'exhibit'
                ORDER BY a.created_seq DESC LIMIT 1
                """,
                (exp_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def _pinned_exhibit(self, exp_id: str) -> dict:
        association = self._exhibit_association(exp_id)
        self.assertIsNotNone(association, "no pinned exhibit association")
        data = self.app._blobs.get(
            namespace=self.project_id, sha256=str(association["content_sha256"])
        )
        return json.loads(data.decode("utf-8"))

    # ---- finalize + pin at submit_results ----

    def test_submit_results_pins_a_system_authored_exhibit(self) -> None:
        exp_id = self._drive_to_running()
        self._log_run("seed-0")
        self._log_run("seed-1")
        self._submit_ready(exp_id)
        out = self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        self.assertEqual(out["status"], "experiment_review")
        self.assertTrue(out["metrics_exhibit"]["pinned"])
        self.assertEqual(out["metrics_exhibit"]["verdict"]["runs_found"], 2)

        exhibit = self._pinned_exhibit(exp_id)
        self.assertEqual([r["run_id"] for r in exhibit["runs"]], ["seed-0", "seed-1"])
        # Result-file source ingested with parsed payload.
        self.assertEqual(exhibit["result_files"][0]["data"], {"accuracy": 0.72})

        association = self._exhibit_association(exp_id)
        self.assertEqual(association["created_by"], "system")
        self.assertTrue(str(association["path"]).endswith("metrics_exhibit.json"))

    def test_runs_logged_after_submit_do_not_exist_for_the_attempt(self) -> None:
        exp_id = self._drive_to_running()
        self._log_run("in-window")
        self._submit_ready(exp_id)
        self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        self._log_run("late-write")
        exhibit = self._pinned_exhibit(exp_id)
        self.assertEqual([r["run_id"] for r in exhibit["runs"]], ["in-window"])

    def test_previous_attempt_runs_stay_out_of_the_window(self) -> None:
        exp_id = self._drive_to_running()
        self.mlflow.runs.append(
            _run("previous-attempt", start_ms=int(time.time() * 1000) - 3_600_000)
        )
        self._log_run("current")
        self._submit_ready(exp_id)
        self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        exhibit = self._pinned_exhibit(exp_id)
        self.assertEqual([r["run_id"] for r in exhibit["runs"]], ["current"])

    def test_report_must_reference_the_exhibit_when_runs_exist(self) -> None:
        exp_id = self._drive_to_running()
        self._log_run("seed-0")
        self._submit_ready(exp_id, report=REPORT_WITHOUT_REFERENCE)
        with self.assertRaises(WorkflowError) as ctx:
            self.call(
                "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
            )
        self.assertIn("metrics_exhibit.json", str(ctx.exception))
        # Referencing the exhibit (after previewing it) unblocks the gate.
        self._write_and_associate(exp_id=exp_id, path="report.md", role="report", body=REPORT_WITH_REFERENCE)
        out = self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        self.assertEqual(out["status"], "experiment_review")

    def test_no_runs_means_no_exhibit_and_no_gate_machinery(self) -> None:
        exp_id = self._drive_to_running()
        self._submit_ready(exp_id, report=REPORT_WITHOUT_REFERENCE)
        out = self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        self.assertEqual(out["status"], "experiment_review")
        self.assertIsNone(self._exhibit_association(exp_id))
        self.assertNotIn("metrics_exhibit", out)

    def test_mlflow_outage_pins_a_visibly_unavailable_exhibit(self) -> None:
        exp_id = self._drive_to_running()
        self._submit_ready(exp_id)
        self.mlflow.available = False
        self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        exhibit = self._pinned_exhibit(exp_id)
        self.assertFalse(exhibit["mlflow"]["available"])
        self.assertEqual(exhibit["runs"], [])

    def test_generation_verdict_is_recorded_for_instrumentation(self) -> None:
        exp_id = self._drive_to_running()
        self._log_run("seed-0")
        self._submit_ready(exp_id)
        self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        conn = self.app._store.connect()
        try:
            row = conn.execute(
                "SELECT payload_json FROM events WHERE type = 'experiment.exhibit_generated' AND target_id = ?",
                (exp_id,),
            ).fetchone()
        finally:
            conn.close()
        payload = json.loads(str(row["payload_json"]))
        self.assertEqual(payload["runs_found"], 1)
        self.assertTrue(payload["pinned"])

    def test_reviewer_hydration_includes_the_exhibit_content(self) -> None:
        # The exhibit exists only as pinned bytes — review.start hydration is
        # the read-only reviewer's one way to see the ground-truth numbers.
        exp_id = self._drive_to_running()
        self._log_run("seed-0")
        self._submit_ready(exp_id)
        self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        req = self.call(
            "review.request",
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp_id,
            role="experiment_reviewer",
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id="experiment_reviewer-reviewer",
        )
        by_role = {a["role"]: a for a in session["submitted_artifacts"]}
        self.assertIn("exhibit", by_role)
        exhibit = json.loads(by_role["exhibit"]["content"])
        self.assertEqual([r["run_id"] for r in exhibit["runs"]], ["seed-0"])

    # ---- preview during running ----

    def test_preview_matches_final_for_identical_state(self) -> None:
        exp_id = self._drive_to_running()
        self._log_run("seed-0")
        self._submit_ready(exp_id)
        preview = self.call("experiment.exhibit", project_id=self.project_id, experiment_id=exp_id)
        self.assertIn("metrics_exhibit.json", preview["exhibit_path"])
        self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        final = self._pinned_exhibit(exp_id)
        # Same generation code, same state: byte-identical record — no
        # generation timestamp lives in the payload.
        self.assertEqual(exhibit_bytes(preview["exhibit"]), exhibit_bytes(final))

    def test_preview_requires_a_running_experiment(self) -> None:
        exp_id = self.call(
            "experiment.create", name="exp-idle", project_id=self.project_id, intent="Preview gate."
        )["id"]
        with self.assertRaises(WorkflowError):
            self.call("experiment.exhibit", project_id=self.project_id, experiment_id=exp_id)

    # ---- agent immutability ----

    def test_agents_cannot_author_replace_or_delete_the_exhibit(self) -> None:
        exp_id = self._drive_to_running()
        self._log_run("seed-0")
        self._submit_ready(exp_id)
        self.call(
            "experiment.transition", project_id=self.project_id, experiment_id=exp_id, transition="submit_results"
        )
        association = self._exhibit_association(exp_id)
        exhibit_path = str(association["path"])

        # The exhibit role is not associable through the agent surface.
        forged = self.repo / "forged.json"
        forged.write_text("{}")
        res = self.call("resource.register", project_id=self.project_id, path="forged.json", kind="result")
        with self.assertRaises(ValidationError):
            self.call(
                "resource.register",
                project_id=self.project_id,
                resource_id=res["id"],
                target_type="experiment",
                target_id=exp_id,
                role="exhibit",
            )
        # The system resource cannot be re-registered over or deleted.
        (self.repo / exhibit_path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / exhibit_path).write_text('{"forged": true}')
        with self.assertRaises(ValidationError):
            self.call("resource.register", project_id=self.project_id, path=exhibit_path, kind="result")
        with self.assertRaises(ValidationError):
            self.call("resource.delete", project_id=self.project_id, resource_id=association["resource_id"])


if __name__ == "__main__":
    unittest.main()
