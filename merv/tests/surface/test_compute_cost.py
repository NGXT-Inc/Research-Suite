"""HTTP surface for the project compute-spend panel.

GET /api/projects/{id}/compute-cost serves the generations-ledger reading
(price × runtime) with experiment names hydrated for display.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.transport.http_api import create_fastapi_app


class ComputeCostEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(create_fastapi_app(self.app))
        self.project_id = self.app.call_tool("project", {"action": "create", "name": "Cost P"})["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _get(self, path: str) -> dict:
        response = self.client.get(path)
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

    def test_empty_project_returns_zeroed_shape(self) -> None:
        body = self._get(f"/api/projects/{self.project_id}/compute-cost")
        self.assertAlmostEqual(body["total_usd"], 0.0)
        self.assertEqual(body["generations"], 0)
        self.assertEqual(body["by_experiment"], [])
        self.assertEqual(body["daily"], [])

    def test_spend_served_with_experiment_names(self) -> None:
        exp_id = self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": "ablation-sweep", "intent": "x"},
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandbox_generations
                  (id, experiment_id, project_id, tenant_id, instance_type,
                   price_usd_per_hour, started_at, ended_at, created_seq)
                VALUES ('sbg_1', ?, ?, 'local', 'gpu_1x_a100', 1.29,
                        '2026-01-01T00:00:00Z', '2026-01-01T02:00:00Z', 0)
                """,
                (exp_id, self.project_id),
            )
        body = self._get(f"/api/projects/{self.project_id}/compute-cost")
        self.assertAlmostEqual(body["total_usd"], 2.58)
        self.assertAlmostEqual(body["total_hours"], 2.0)
        self.assertEqual(body["open_generations"], 0)
        self.assertEqual(len(body["by_experiment"]), 1)
        self.assertEqual(body["by_experiment"][0]["experiment_id"], exp_id)
        self.assertEqual(body["by_experiment"][0]["experiment_name"], "ablation-sweep")
        self.assertEqual(body["by_hardware"][0]["instance_type"], "gpu_1x_a100")
        self.assertEqual([d["date"] for d in body["daily"]], ["2026-01-01"])


if __name__ == "__main__":
    unittest.main()
