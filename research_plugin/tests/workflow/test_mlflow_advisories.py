"""Advisory anomaly detection: pure detectors and the observation flow.

The detector half needs no brain: advisories are deterministic over the
bounded snapshot shape. The flow half drives the real HTTP/tool surfaces to
prove the contract: advisories ride the metrics payloads, each newly seen one
becomes exactly one durable ``experiment.mlflow_advisory`` event, repeated
reads stay silent, cleared problems drop from the stored set, and the
constantly polled ``workflow.status_and_next`` carries them while running —
as observations, never instructions.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from backend.mlflow import CentralMlflowService
from backend.mlflow.advisories import (
    advisory_fingerprint,
    detect_run_advisories,
    detect_snapshot_advisories,
    good_direction,
)
from backend.transport.http_api import create_fastapi_app


def _run(*, history, params=None, status="RUNNING", run_id="r1", run_name="seed_0"):
    return {
        "run_id": run_id,
        "run_name": run_name,
        "status": status,
        "start_time": 1_000,
        "params": params or {},
        "metrics": {},
        "history": history,
    }


def _series(values, *, key="loss"):
    return {key: [[step, value] for step, value in enumerate(values)]}


DIVERGING_LOSS = [1.0, 0.5, 0.3, 0.2, 0.2, 0.5, 0.8, 0.9, 1.0, 1.1]
IMPROVING_LOSS = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]


class AdvisoryDetectorTest(unittest.TestCase):
    def test_healthy_improving_loss_yields_nothing(self) -> None:
        run = _run(history=_series(IMPROVING_LOSS))
        self.assertEqual(detect_run_advisories(run), [])

    def test_non_finite_values_are_a_warning(self) -> None:
        run = _run(history={"loss": [[1, 0.5], [2, None], [3, 0.4]]})
        advisories = detect_run_advisories(run)
        self.assertEqual([a["code"] for a in advisories], ["non_finite_values"])
        advisory = advisories[0]
        self.assertEqual(advisory["severity"], "warning")
        self.assertEqual(advisory["metric"], "loss")
        self.assertEqual(advisory["evidence"]["first_non_finite_step"], 2)
        self.assertIn("step 2", advisory["reasoning"])

    def test_down_good_metric_moving_off_its_best_diverges(self) -> None:
        advisories = detect_run_advisories(_run(history=_series(DIVERGING_LOSS)))
        self.assertEqual([a["code"] for a in advisories], ["metric_diverging"])
        advisory = advisories[0]
        self.assertEqual(advisory["severity"], "warning")
        self.assertEqual(advisory["evidence"]["best"], 0.2)
        self.assertEqual(advisory["evidence"]["best_step"], 3)
        # The message carries the numbers, not an instruction.
        self.assertIn("moving away from its best", advisory["summary"])
        self.assertNotIn("kill", advisory["reasoning"].lower())

    def test_up_good_metric_falling_from_its_best_diverges(self) -> None:
        values = [0.2, 0.4, 0.6, 0.8, 0.9, 0.9, 0.6, 0.5, 0.5, 0.45]
        advisories = detect_run_advisories(_run(history=_series(values, key="val_acc")))
        self.assertEqual([a["code"] for a in advisories], ["metric_diverging"])
        self.assertEqual(advisories[0]["evidence"]["best"], 0.9)

    def test_plateau_flags_running_runs_only(self) -> None:
        values = [2.0, 1.7, 1.4, 1.1, 0.9, 0.7, 0.6, 0.55, 0.52, 0.5,
                  0.5, 0.501, 0.499, 0.5, 0.5, 0.501, 0.5, 0.499, 0.5, 0.5]
        live = detect_run_advisories(_run(history=_series(values), status="RUNNING"))
        self.assertEqual([a["code"] for a in live], ["metric_plateau"])
        self.assertEqual(live[0]["severity"], "notice")
        finished = detect_run_advisories(_run(history=_series(values), status="FINISHED"))
        self.assertEqual(finished, [])

    def test_declared_direction_beats_unknown_name(self) -> None:
        # "energy" matches no convention; the run-declared contract makes it
        # down-good and the diverging shape detectable.
        params = {"primary_metric": "energy", "primary_metric_direction": "minimize"}
        silent = detect_run_advisories(_run(history=_series(DIVERGING_LOSS, key="energy")))
        self.assertEqual(silent, [])
        declared = detect_run_advisories(
            _run(history=_series(DIVERGING_LOSS, key="energy"), params=params)
        )
        self.assertEqual([a["code"] for a in declared], ["metric_diverging"])
        self.assertEqual(good_direction("energy", params), -1)

    def test_exit_codes_never_read_as_trends(self) -> None:
        values = [0, 0, 0, 0, 0, 0, 0, 0, 0, 1]
        run = _run(history=_series(values, key="eval_exit_code"))
        self.assertEqual(detect_run_advisories(run), [])

    def test_snapshot_detection_respects_the_attempt_window(self) -> None:
        stale = _run(history=_series(DIVERGING_LOSS), run_id="old")
        stale["start_time"] = 500
        fresh = _run(history=_series(DIVERGING_LOSS), run_id="new")
        fresh["start_time"] = 2_000
        snapshot = {
            "available": True,
            "experiments": [{"name": "rp/p/e", "runs": [stale, fresh]}],
        }
        advisories = detect_snapshot_advisories(snapshot, window_started_ms=1_000)
        self.assertEqual([a["run_id"] for a in advisories], ["new"])
        unwindowed = detect_snapshot_advisories(snapshot)
        self.assertEqual({a["run_id"] for a in unwindowed}, {"old", "new"})

    def test_fingerprint_is_stable_identity(self) -> None:
        [advisory] = detect_run_advisories(_run(history=_series(DIVERGING_LOSS)))
        self.assertEqual(advisory_fingerprint(advisory), "r1:loss:metric_diverging")


class AdvisoryFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.client = TestClient(create_fastapi_app(self.app))
        project = self.request("POST", "/api/projects", {"name": "Advisory Project"})
        self.project_id = project["id"]
        exp = self.request(
            "POST",
            f"/api/projects/{self.project_id}/experiments",
            {"name": "train-run", "intent": "Train."},
        )
        self.exp_id = exp["id"]
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.test",
            health_check=lambda: True,
        )
        for field in ("mode", "tracking_uri", "_control_uri", "server_uri", "dashboard_url", "note", "_health_check"):
            setattr(self.app.mlflow_tracking, field, getattr(service, field))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        response = self.client.request(method, path, json=body)
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

    def _snapshot(self, values):
        return {
            "source": "mlflow",
            "experiments": [
                {
                    "experiment_id": "7",
                    "name": f"rp/{self.project_id}/{self.exp_id}",
                    "runs": [_run(history=_series(values))],
                }
            ],
        }

    def _advisory_events(self):
        events = self.request("GET", f"/api/projects/{self.project_id}/events?limit=500")
        return [e for e in events["events"] if e["type"] == "experiment.mlflow_advisory"]

    def _read_metrics(self, values):
        with patch("backend.mlflow.tracking.snapshot_mlflow", return_value=self._snapshot(values)):
            return self.request(
                "GET",
                f"/api/projects/{self.project_id}/experiments/{self.exp_id}/results/metrics",
            )

    def test_advisories_ride_reads_and_events_fire_once(self) -> None:
        payload = self._read_metrics(DIVERGING_LOSS)
        self.assertEqual([a["code"] for a in payload["advisories"]], ["metric_diverging"])
        self.assertIn("observations, not instructions", payload["advisory_note"])

        events = self._advisory_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["target_id"], self.exp_id)
        self.assertEqual(events[0]["payload"]["code"], "metric_diverging")

        # The same unchanged observation writes nothing new.
        self._read_metrics(DIVERGING_LOSS)
        self.assertEqual(len(self._advisory_events()), 1)

        # The stored set rides get_state and the running orientation call.
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'running' WHERE id = ?", (self.exp_id,)
            )
        state = self.app.call_tool(
            "experiment.get_state",
            {"project_id": self.project_id, "experiment_id": self.exp_id},
        )
        self.assertEqual(
            [a["code"] for a in state["mlflow_advisories"]], ["metric_diverging"]
        )
        status = self.app.call_tool(
            "workflow.status_and_next",
            {"project_id": self.project_id, "experiment_id": self.exp_id},
        )
        items = status["experiment"]["mlflow_advisories"]
        self.assertEqual([a["code"] for a in items], ["metric_diverging"])
        self.assertTrue(items[0]["first_detected_at"])
        self.assertIn("observations, not instructions", status["mlflow_advisory_note"])

        # A healthy read clears the stored set (the event history remains).
        healthy = self._read_metrics(IMPROVING_LOSS)
        self.assertNotIn("advisories", healthy)
        status = self.app.call_tool(
            "workflow.status_and_next",
            {"project_id": self.project_id, "experiment_id": self.exp_id},
        )
        self.assertNotIn("mlflow_advisories", status["experiment"])
        self.assertNotIn("mlflow_advisory_note", status)
        self.assertEqual(len(self._advisory_events()), 1)

    def test_unavailable_mlflow_does_not_clear_the_stored_set(self) -> None:
        self._read_metrics(DIVERGING_LOSS)
        with patch("backend.mlflow.tracking.snapshot_mlflow", return_value=None):
            unavailable = self.request(
                "GET",
                f"/api/projects/{self.project_id}/experiments/{self.exp_id}/results/metrics",
            )
        self.assertFalse(unavailable["available"])
        state = self.app.call_tool(
            "experiment.get_state",
            {"project_id": self.project_id, "experiment_id": self.exp_id},
        )
        self.assertEqual(
            [a["code"] for a in state["mlflow_advisories"]], ["metric_diverging"]
        )

    def test_exhibit_preview_carries_the_same_observations(self) -> None:
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'running' WHERE id = ?", (self.exp_id,)
            )
        with patch(
            "backend.mlflow.tracking.snapshot_mlflow",
            return_value=self._snapshot(DIVERGING_LOSS),
        ):
            preview = self.app.call_tool(
                "experiment.exhibit",
                {"project_id": self.project_id, "experiment_id": self.exp_id},
            )
        self.assertEqual(
            [a["code"] for a in preview["advisories"]], ["metric_diverging"]
        )
        self.assertIn("observations, not instructions", preview["advisory_note"])
        self.assertEqual(len(self._advisory_events()), 1)


if __name__ == "__main__":
    unittest.main()
