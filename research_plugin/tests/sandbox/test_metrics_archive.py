from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.mlflow_metrics import MAX_HISTORY_POINTS, downsample_history, snapshot_mlflow


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class FakeClient:
    """Routes MLflow REST paths to canned payloads; no network."""

    def __init__(self, *, experiments=None, runs=None, history=None, fail=False) -> None:
        self.experiments = experiments or []
        self.runs = runs or {}
        self.history = history or {}
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        if self.fail:
            raise OSError("connection refused")
        if url.endswith("/experiments/search"):
            experiments = self.experiments
            wanted = (params or {}).get("filter", "")
            if wanted.startswith("name = '"):
                name = wanted.removeprefix("name = '").removesuffix("'")
                experiments = [e for e in experiments if e.get("name") == name]
            return FakeResponse({"experiments": experiments})
        if url.endswith("/metrics/get-history"):
            key = (params or {}).get("run_id"), (params or {}).get("metric_key")
            return FakeResponse({"metrics": self.history.get(key, [])})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, json=None):
        if self.fail:
            raise OSError("connection refused")
        if url.endswith("/runs/search"):
            experiment_id = (json or {}).get("experiment_ids", [""])[0]
            return FakeResponse({"runs": self.runs.get(experiment_id, [])})
        raise AssertionError(f"unexpected POST {url}")


def _client_patch(client: FakeClient):
    return patch(
        "backend.mlflow_metrics.httpx.Client",
        return_value=client,
    )


class SnapshotMlflowTest(unittest.TestCase):
    def test_snapshot_captures_runs_params_metrics_history(self) -> None:
        client = FakeClient(
            experiments=[
                {"experiment_id": "0", "name": "Default", "last_update_time": 1},
                {"experiment_id": "1", "name": "lora_glue", "last_update_time": 99},
            ],
            runs={
                # Default has no runs and must be skipped entirely.
                "0": [],
                "1": [
                    {
                        "info": {
                            "run_id": "r1",
                            "run_name": "seed_0",
                            "status": "FINISHED",
                            "start_time": 100,
                            "end_time": 200,
                        },
                        "data": {
                            "params": [{"key": "lr", "value": "0.0005"}],
                            "metrics": [
                                {"key": "acc", "value": 0.91, "step": 20, "timestamp": 5},
                                {"key": "bad", "value": float("nan"), "step": 1, "timestamp": 5},
                            ],
                        },
                    }
                ],
            },
            history={
                ("r1", "acc"): [
                    {"step": 10, "value": 0.85},
                    {"step": 20, "value": 0.91},
                ],
                ("r1", "bad"): [],
            },
        )
        with _client_patch(client):
            snapshot = snapshot_mlflow("http://127.0.0.1:5000/#/experiments/1")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["base_url"], "http://127.0.0.1:5000")
        self.assertEqual(len(snapshot["experiments"]), 1)
        exp = snapshot["experiments"][0]
        self.assertEqual(exp["name"], "lora_glue")
        run = exp["runs"][0]
        self.assertEqual(run["run_name"], "seed_0")
        self.assertEqual(run["params"], {"lr": "0.0005"})
        self.assertEqual(run["metrics"]["acc"]["last"], 0.91)
        self.assertEqual(run["metrics"]["acc"]["min"], 0.85)
        self.assertEqual(run["metrics"]["acc"]["max"], 0.91)
        self.assertEqual(run["history"]["acc"], [[10, 0.85], [20, 0.91]])
        self.assertIsNone(run["metrics"]["bad"]["last"])
        self.assertNotIn("bad", run["history"])
        # The whole record must be strict JSON (no NaN literals).
        json.loads(json.dumps(snapshot, allow_nan=False))

    def test_snapshot_can_scope_to_one_experiment_name(self) -> None:
        client = FakeClient(
            experiments=[
                {"experiment_id": "1", "name": "rp/proj_a/exp_a"},
                {"experiment_id": "2", "name": "rp/proj_b/exp_b"},
            ],
            runs={
                "1": [
                    {
                        "info": {"run_id": "r1", "run_name": "a"},
                        "data": {"metrics": [{"key": "acc", "value": 0.1}]},
                    }
                ],
                "2": [
                    {
                        "info": {"run_id": "r2", "run_name": "b"},
                        "data": {"metrics": [{"key": "acc", "value": 0.9}]},
                    }
                ],
            },
        )
        with _client_patch(client):
            snapshot = snapshot_mlflow(
                "https://mlflow.test", experiment_name="rp/proj_b/exp_b"
            )
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot["experiments"]), 1)
        self.assertEqual(snapshot["experiments"][0]["name"], "rp/proj_b/exp_b")
        self.assertEqual(
            snapshot["experiments"][0]["runs"][0]["metrics"]["acc"]["last"], 0.9
        )

    def test_snapshot_none_when_no_runs_anywhere(self) -> None:
        client = FakeClient(
            experiments=[{"experiment_id": "0", "name": "Default", "last_update_time": 1}],
            runs={"0": []},
        )
        with _client_patch(client):
            self.assertIsNone(snapshot_mlflow("http://x"))

    def test_snapshot_none_when_unreachable_or_blank(self) -> None:
        with _client_patch(FakeClient(fail=True)):
            self.assertIsNone(snapshot_mlflow("http://x"))
        self.assertIsNone(snapshot_mlflow(""))

    def test_downsample_caps_points_and_keeps_endpoints(self) -> None:
        points = [[i, float(i)] for i in range(5000)]
        sampled = downsample_history(points)
        self.assertLessEqual(len(sampled), MAX_HISTORY_POINTS + 1)
        self.assertEqual(sampled[0], [0, 0.0])
        self.assertEqual(sampled[-1], [4999, 4999.0])
        short = [[0, 1.0], [1, 2.0]]
        self.assertEqual(downsample_history(short), short)


if __name__ == "__main__":
    unittest.main()
