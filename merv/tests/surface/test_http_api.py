from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain, upload_token
from merv.brain.mlflow import CentralMlflowService
from merv.brain.surface.transport.http_api import create_fastapi_app
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.proxy.time_utils import now_iso


class ResearchPluginHttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.client = TestClient(create_fastapi_app(self.app.http))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        response = self.client.request(method, path, json=body)
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

    def submit(
        self,
        *,
        pid: str,
        target_type: str,
        target_id: str,
        role: str,
        path: str,
        body: str,
        lens_id: str = "",
    ) -> dict:
        return self.app.submit_artifact(
            project_id=pid,
            target_type=target_type,
            target_id=target_id,
            role=role,
            path=path,
            body=body,
            lens_id=lens_id,
        )

    def configure_mlflow(self, service: CentralMlflowService) -> None:
        self.app.mlflow_tracking.mode = service.mode
        self.app.mlflow_tracking.tracking_uri = service.tracking_uri
        self.app.mlflow_tracking._control_uri = service._control_uri
        self.app.mlflow_tracking.server_uri = service.server_uri
        self.app.mlflow_tracking.dashboard_url = service.dashboard_url
        self.app.mlflow_tracking.note = service.note
        self.app.mlflow_tracking._health_check = service._health_check

    def test_home_claim_experiment_artifact_review_endpoints(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "UI Project", "summary": "Frontend target"})
        project_id = project["id"]
        claim = self.request(
            "POST",
            f"/api/projects/{project_id}/claims",
            {"statement": "Threshold classifier improves toy accuracy."},
        )
        exp = self.request(
            "POST",
            f"/api/projects/{project_id}/experiments",
            {"name": "threshold-vs-baseline", "intent": "Compare threshold with baseline.", "claim_ids": [claim["id"]]},
        )
        exp_id = exp["id"]
        first = self.submit(
            pid=project_id,
            target_type="experiment",
            target_id=exp_id,
            role="plan",
            path="plan.md",
            body=(
                "## Summary\nCompare a threshold classifier with the baseline.\n\n"
                "## Objective & hypothesis\nThreshold rule beats majority class.\n\n"
                "## Evaluation\nMetric: accuracy vs majority baseline; success if higher.\n"
            ),
        )

        home = self.request("GET", f"/api/projects/{project_id}/home")
        self.assertEqual(home["project"]["name"], "UI Project")
        self.assertEqual(home["stats"]["claims"], 1)
        self.assertEqual(home["workflow"]["next_action"], "submit_design_for_review")

        content = self.request(
            "GET", f"/api/projects/{project_id}/artifacts/{first['artifact_id']}/content"
        )
        self.assertIn("accuracy", content["content"])
        # Resubmitting the same slot supersedes: a new artifact id, old row gone.
        second = self.submit(
            pid=project_id,
            target_type="experiment",
            target_id=exp_id,
            role="plan",
            path="plan.md",
            body=(
                "## Summary\nCompare a threshold classifier with the baseline (v2).\n\n"
                "## Objective & hypothesis\nThreshold rule beats majority class.\n\n"
                "## Evaluation\nMetric: accuracy vs majority baseline; success if higher.\n"
            ),
        )
        artifact_id = second["artifact_id"]
        self.assertNotEqual(artifact_id, first["artifact_id"])
        listing = self.request("GET", f"/api/projects/{project_id}/artifacts")
        self.assertEqual(
            [row["id"] for row in listing["artifacts"]], [artifact_id]
        )

        self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/transition", {"transition": "submit_design"})
        review_request = self.request(
            "POST",
            f"/api/projects/{project_id}/reviews/request",
            {"target_type": "experiment", "target_id": exp_id, "role": "design_reviewer"},
        )
        self.assertEqual(review_request["role"], "design_reviewer")
        self.assertEqual(review_request["target_snapshot"]["artifacts"][0]["artifact_id"], artifact_id)
        reviews = self.request("GET", f"/api/projects/{project_id}/reviews?target_type=experiment&target_id={exp_id}")
        self.assertEqual(len(reviews["requests"]), 1)
        self.assertEqual(reviews["requests"][0]["target_snapshot"]["artifacts"][0]["artifact_id"], artifact_id)
        queue = self.request("GET", f"/api/projects/{project_id}/reviews")
        self.assertEqual(queue["requests"][0]["target_snapshot"]["artifacts"][0]["artifact_id"], artifact_id)

        # The synopsis is the researcher's TLDR: it persists and surfaces on
        # both the target-scoped review.status view and the project-wide queue.
        session = self.request(
            "POST",
            f"/api/projects/{project_id}/reviews/start",
            {
                "review_request_id": review_request["review_request_id"],
                "reviewer_capability": review_request["reviewer_capability"],
                "caller_session_id": "home-endpoint-reviewer",
            },
        )
        synopsis = "The threshold rule clears the majority-class baseline, so the design is sound."
        self.request(
            "POST",
            f"/api/projects/{project_id}/reviews/submit",
            {"review_session_id": session["review_session_id"], "verdict": "pass", "synopsis": synopsis},
        )
        conn = self.app._store.connect()
        try:
            cursor = int(conn.execute("SELECT MAX(id) AS id FROM events").fetchone()["id"])
        finally:
            conn.close()
        with patch.object(
            self.app.reaction_registry,
            "dispatch",
            wraps=self.app.reaction_registry.dispatch,
        ) as dispatch:
            tool_status = self.app.call_tool(
                "review.status",
                {"project_id": project_id, "target_type": "experiment", "target_id": exp_id},
            )
            status = self.request("GET", f"/api/projects/{project_id}/reviews?target_type=experiment&target_id={exp_id}")
        self.assertEqual(status, tool_status)
        self.assertIn("feed_note", status)
        review_dispatches = [
            call.kwargs for call in dispatch.call_args_list
            if call.kwargs["event"].type == "review.submitted"
        ]
        self.assertEqual(len(review_dispatches), 2)
        self.assertEqual(
            [(call["event"].id, call["phase"]) for call in review_dispatches],
            [(cursor, "producer_read"), (cursor, "producer_read")],
        )
        self.assertEqual(
            review_dispatches[0]["event"].payload["review_id"],
            status["reviews"][0]["id"],
        )
        conn = self.app._store.connect()
        try:
            self.assertEqual(
                int(conn.execute("SELECT MAX(id) AS id FROM events").fetchone()["id"]),
                cursor,
            )
        finally:
            conn.close()
        self.assertEqual(status["reviews"][0]["synopsis"], synopsis)
        queue = self.request("GET", f"/api/projects/{project_id}/reviews")
        self.assertEqual(queue["reviews"][0]["synopsis"], synopsis)

    def test_review_start_and_submit_are_scoped_to_route_project(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Scoped A"})
        pid = project["id"]
        exp = self.request("POST", f"/api/projects/{pid}/experiments", {"name": "exp-1", "intent": "Scoped review"})
        exp_id = exp["id"]
        self.submit(
            pid=pid,
            target_type="experiment",
            target_id=exp_id,
            role="plan",
            path="plan.md",
            body=(
                "## Summary\nScoped review.\n\n"
                "## Objective & hypothesis\nTest scoping.\n\n"
                "## Evaluation\nMetric: pass/fail of the scoping check.\n"
            ),
        )
        self.request("POST", f"/api/projects/{pid}/experiments/{exp_id}/transition", {"transition": "submit_design"})
        req = self.request("POST", f"/api/projects/{pid}/reviews/request", {"target_type": "experiment", "target_id": exp_id, "role": "design_reviewer"})

        other = self.request("POST", "/api/projects", {"name": "Scoped B"})
        other_id = other["id"]

        # Starting the review under the wrong project's URL is rejected.
        wrong_start = self.client.request(
            "POST",
            f"/api/projects/{other_id}/reviews/start",
            json={"review_request_id": req["review_request_id"], "reviewer_capability": req["reviewer_capability"], "caller_session_id": "rev"},
        )
        self.assertEqual(wrong_start.status_code, 404, wrong_start.text)

        # The correct project still works.
        session = self.request(
            "POST",
            f"/api/projects/{pid}/reviews/start",
            {"review_request_id": req["review_request_id"], "reviewer_capability": req["reviewer_capability"], "caller_session_id": "rev"},
        )
        recorded = self.app.tool_calls.stats(tool="review.start")
        self.assertEqual(recorded["totals"]["calls"], 1)
        self.assertEqual(recorded["calls"][0]["tool"], "review.start")
        self.assertEqual(recorded["calls"][0]["project_id"], pid)

        wrong_submit = self.client.request(
            "POST",
            f"/api/projects/{other_id}/reviews/submit",
            json={
                "review_session_id": session["review_session_id"],
                "verdict": "pass",
                "synopsis": "The plan and results check out, so the attempt stands as reported.",
            },
        )
        self.assertEqual(wrong_submit.status_code, 404, wrong_submit.text)

        # Submitting under the owning project still works.
        self.request(
            "POST",
            f"/api/projects/{pid}/reviews/submit",
            {
                "review_session_id": session["review_session_id"],
                "verdict": "pass",
                "synopsis": "The plan and results check out, so the attempt stands as reported.",
            },
        )

    def test_claim_update_http_endpoint(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Claim Update"})
        pid = project["id"]
        claim = self.request("POST", f"/api/projects/{pid}/claims", {"statement": "X improves Y."})
        updated = self.request("PATCH", f"/api/projects/{pid}/claims/{claim['id']}", {"status": "supported", "confidence": "high"})
        self.assertEqual(updated["status"], "supported")
        self.assertEqual(updated["confidence"], "high")

    def test_sandbox_http_endpoints(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Sandbox UI Project"})
        project_id = project["id"]
        exp = self.request("POST", f"/api/projects/{project_id}/experiments", {"name": "exp-2", "intent": "Run an experiment"})
        exp_id = exp["id"]
        # Keep the rest of the endpoint fixture in the usual runnable state.
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,))
        # Procuring is an agent action (MCP tool); the UI observes the result.
        requested = self.app.call_tool(
            "sandbox.request", {"project_id": project_id, "experiment_id": exp_id, "gpu": "A100"}
        )
        self.assertEqual(requested["status"], "running")
        sandbox_uid = requested["sandbox_uid"]
        self.assertIn("host", requested["ssh"])
        self.assertNotIn("command", requested["ssh"])
        self.assertNotIn("raw_command", requested["ssh"])

        sandbox = self.request("GET", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox")
        self.assertEqual(sandbox["status"], "running")
        self.assertTrue(sandbox["sandbox_id"])
        self.assertNotIn("dashboards", sandbox)
        sandbox_by_uid = self.request(
            "GET", f"/api/projects/{project_id}/sandboxes/{sandbox_uid}"
        )
        self.assertEqual(sandbox_by_uid["sandbox_uid"], sandbox_uid)
        self.assertEqual(sandbox_by_uid["status"], "running")

        listed = self.request("GET", f"/api/projects/{project_id}/sandboxes")["sandboxes"]
        self.assertEqual(len(listed), 1)

        # Live usage metrics endpoint surfaces the in-container sample.
        self.backend.metrics[requested["sandbox_id"]] = {
            "cpu": {"used_cores": 1.0, "limit_cores": 2.0},
            "memory": {"used_bytes": 1073741824, "limit_bytes": 8589934592},
            "gpus": [{"index": 0, "name": "A100", "util_pct": 10, "mem_used_mib": 512, "mem_total_mib": 40960}],
        }
        metrics = self.request("GET", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/metrics")
        self.assertTrue(metrics["available"])
        self.assertEqual(metrics["metrics"]["gpus"][0]["util_pct"], 10)
        metrics_by_uid = self.request(
            "GET", f"/api/projects/{project_id}/sandboxes/{sandbox_uid}/metrics"
        )
        self.assertTrue(metrics_by_uid["available"])
        self.assertEqual(metrics_by_uid["metrics"]["gpus"][0]["util_pct"], 10)

        self.backend.append_transcript(experiment_id=exp_id, text="$ ls\nplan.md\n")
        terminal = self.request("GET", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/terminal")
        self.assertIn("plan.md", terminal["transcript"])
        # Incremental polling: `since=cursor` returns only new bytes.
        cursor = terminal["cursor"]
        unchanged = self.request(
            "GET",
            f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/terminal?since={cursor}",
        )
        self.assertEqual(unchanged["transcript"], "")
        self.assertEqual(unchanged["cursor"], cursor)
        self.backend.append_transcript(experiment_id=exp_id, text="results.json\n")
        delta = self.request(
            "GET",
            f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/terminal?since={cursor}",
        )
        self.assertEqual(delta["transcript"], "results.json\n")
        self.assertGreater(delta["cursor"], cursor)

        self.app.sandboxes.transcript_cache.invalidate(sandbox_id=requested["sandbox_id"])
        self.backend.append_transcript(experiment_id=sandbox_uid, text="uid transcript\n")
        terminal_by_uid = self.request(
            "GET", f"/api/projects/{project_id}/sandboxes/{sandbox_uid}/terminal"
        )
        self.assertIn("uid transcript", terminal_by_uid["transcript"])

        released = self.request("POST", f"/api/projects/{project_id}/sandboxes/{sandbox_uid}/release")
        self.assertEqual(released["status"], "terminated")

        self.assertTrue(self.request("GET", "/api/sandboxes/health")["ok"])

    def test_results_metrics_endpoint_reads_central_mlflow_across_release(self) -> None:
        # Results metrics are centralized MLflow reads, independent of sandbox
        # release/reap.
        project = self.request("POST", "/api/projects", {"name": "Results Project"})
        project_id = project["id"]
        exp = self.request(
            "POST", f"/api/projects/{project_id}/experiments", {"name": "exp-3", "intent": "Train"}
        )
        exp_id = exp["id"]
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,))
        self.app.call_tool(
            "sandbox.request", {"project_id": project_id, "experiment_id": exp_id, "gpu": "A100"}
        )
        mlflow = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.test",
            health_check=lambda: True,
        )
        self.configure_mlflow(mlflow)

        url = f"/api/projects/{project_id}/experiments/{exp_id}/results/metrics"
        with patch("merv.brain.mlflow.tracking.snapshot_mlflow", return_value=None):
            empty = self.request("GET", url)
        self.assertFalse(empty["available"])
        self.assertIn("hint", empty)

        snapshot = {
            "source": "mlflow",
            "base_url": "https://mlflow-x.modal.test",
            "experiments": [
                {
                    "experiment_id": "1",
                    "name": "lora_glue",
                    "runs": [
                        {
                            "run_id": "r1",
                            "run_name": "seed_0",
                            "status": "FINISHED",
                            "params": {"lr": "0.0005"},
                            "metrics": {"acc": {"last": 0.91}},
                            "history": {"acc": [[10, 0.85], [20, 0.91]]},
                        }
                    ],
                }
            ],
        }
        with patch("merv.brain.mlflow.tracking.snapshot_mlflow", return_value=snapshot):
            live = self.request("GET", url)
        self.assertTrue(live["available"])
        self.assertNotIn("base_url", live)

        self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/release")
        with patch("merv.brain.mlflow.tracking.snapshot_mlflow", return_value=snapshot):
            durable = self.request("GET", url)
        self.assertTrue(durable["available"])
        run = durable["experiments"][0]["runs"][0]
        self.assertEqual(run["metrics"]["acc"]["last"], 0.91)
        self.assertEqual(run["history"]["acc"], [[10, 0.85], [20, 0.91]])

    def test_project_mlflow_overview_scopes_runs_and_deep_links(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "MLflow Project"})
        project_id = project["id"]
        exp = self.request(
            "POST", f"/api/projects/{project_id}/experiments", {"name": "exp-ml", "intent": "Train"}
        )
        exp_id = exp["id"]
        mlflow = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.test",
            dashboard_url="https://mlflow.test",
            health_check=lambda: True,
        )
        self.configure_mlflow(mlflow)
        snapshot = {
            "source": "mlflow",
            "experiments": [
                {
                    "experiment_id": "7",
                    "name": f"merv/{project_id}/{exp_id}",
                    "runs": [
                        {
                            "run_id": "r1",
                            "run_name": "seed_0",
                            "status": "FINISHED",
                            "params": {"lr": "0.001"},
                            "metrics": {"acc": {"last": 0.92}},
                        }
                    ],
                }
            ],
        }

        namespace_rows = [
            {"name": f"merv/{project_id}/{exp_id}", "experiment_id": "7"},
            {"name": f"merv/{project_id}/stray", "experiment_id": "8"},
        ]
        namespace = [
            {
                "name": f"merv/{project_id}/{exp_id}",
                "experiment_id": "7",
                "dashboard_experiment_url": "https://mlflow.test/#/experiments/7",
            },
            {
                "name": f"merv/{project_id}/stray",
                "experiment_id": "8",
                "dashboard_experiment_url": "https://mlflow.test/#/experiments/8",
            },
        ]
        run_record = snapshot["experiments"][0]["runs"][0]
        with patch(
            "merv.brain.mlflow.tracking.snapshot_mlflow_project",
            return_value=(namespace_rows, {"7": [run_record]}),
        ) as project_snapshot:
            overview = self.request("GET", f"/api/projects/{project_id}/mlflow")
        self.assertTrue(overview["mlflow"]["configured"])
        self.assertEqual(overview["mlflow"]["dashboard_url"], "https://mlflow.test")
        items = overview["experiments"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["experiment_id"], exp_id)
        self.assertEqual(item["name"], "exp-ml")
        # Deep link resolves the MLflow numeric id from the matching snapshot.
        self.assertEqual(item["dashboard_experiment_url"], "https://mlflow.test/#/experiments/7")
        self.assertEqual(
            list(item["metrics"]),
            [
                "experiment_id",
                "available",
                "source",
                "experiments",
                "dashboard_experiment_url",
            ],
        )
        self.assertEqual(
            item["metrics"],
            {
                "experiment_id": exp_id,
                "available": True,
                "source": "mlflow",
                "experiments": [
                    {
                        "experiment_id": "7",
                        "name": f"merv/{project_id}/{exp_id}",
                        "last_update_time": None,
                        "runs": [run_record],
                    }
                ],
                "dashboard_experiment_url": (
                    "https://mlflow.test/#/experiments/7"
                ),
            },
        )
        run = item["metrics"]["experiments"][0]["runs"][0]
        self.assertNotIn("history", run)
        project_snapshot.assert_called_once_with(
            "https://mlflow.test",
            name_like=f"merv/{project_id}/%",
            experiment_names=frozenset({f"merv/{project_id}/{exp_id}"}),
        )
        self.assertEqual(
            overview["unmapped_mlflow_experiments"], [namespace[1]]
        )

    def test_project_mlflow_overview_short_circuits_when_unreachable(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "MLflow Down"})
        project_id = project["id"]
        exp = self.request(
            "POST",
            f"/api/projects/{project_id}/experiments",
            {"name": "exp-ml", "intent": "Train"},
        )
        self.configure_mlflow(CentralMlflowService(
            tracking_uri="https://mlflow.test",
            health_check=lambda: False,
        ))

        with (
            patch.object(
                self.app.mlflow_tracking, "project_results_snapshot"
            ) as project_snapshot,
        ):
            overview = self.request("GET", f"/api/projects/{project_id}/mlflow")

        project_snapshot.assert_not_called()
        self.assertFalse(overview["mlflow"]["reachable"])
        self.assertFalse(overview["experiments"][0]["metrics"]["available"])
        self.assertEqual(
            overview["experiments"][0]["experiment_id"], exp["id"]
        )
        self.assertEqual(overview["unmapped_mlflow_experiments"], [])

    def test_running_transition_and_tool_hand_mlflow_block(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "ML Run Project"})
        project_id = project["id"]
        exp = self.request(
            "POST", f"/api/projects/{project_id}/experiments", {"name": "exp-run", "intent": "Train"}
        )
        exp_id = exp["id"]
        mlflow = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.test",
            server_uri="http://mlflow.internal:5000",
            dashboard_url="https://mlflow.test",
            health_check=lambda: True,
        )
        self.configure_mlflow(mlflow)
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,))

        before_start = self.app.call_tool(
            "experiment.get_state",
            {"project_id": project_id, "experiment_id": exp_id},
        )
        self.assertNotIn("mlflow", before_start)

        # Transitioning into running hands back the MLflow connection block.
        run_created = {
            "created": True,
            "experiment_name": f"merv/{project_id}/{exp_id}",
            "experiment_id": "7",
            "run_id": "run_123",
            "run_name": f"{exp_id}-attempt-1",
            "status": "RUNNING",
            "artifact_uri": "s3://mlflow/run_123",
            "created_at": "2026-07-02T12:00:00Z",
            "dashboard_run_url": "https://mlflow.test/#/experiments/7/runs/run_123",
        }
        with patch.object(CentralMlflowService, "create_run", return_value=run_created) as create_run:
            transitioned = self.app.call_tool(
                "experiment.transition",
                {"project_id": project_id, "experiment_id": exp_id, "transition": "start_running"},
            )
        create_run.assert_called_once_with(
            project_id=project_id,
            experiment_id=exp_id,
            attempt_index=1,
            run_name=f"{exp_id}-attempt-1",
        )
        self.assertEqual(transitioned["status"], "running")
        self.assertTrue(transitioned["mlflow"]["configured"])
        self.assertEqual(transitioned["mlflow"]["experiment_name"], f"merv/{project_id}/{exp_id}")
        self.assertEqual(transitioned["mlflow"]["env"]["MLFLOW_TRACKING_URI"], "https://mlflow.test")
        self.assertEqual(transitioned["mlflow"]["run"]["run_id"], "run_123")
        self.assertEqual(transitioned["mlflow"]["env"]["MLFLOW_RUN_ID"], "run_123")
        self.assertIn("MLflow", transitioned["mlflow_guidance"])

        state = self.app.call_tool(
            "experiment.get_state",
            {"project_id": project_id, "experiment_id": exp_id},
        )
        self.assertTrue(state["mlflow"]["configured"])
        self.assertEqual(state["mlflow"]["experiment_name"], f"merv/{project_id}/{exp_id}")
        self.assertEqual(state["mlflow_run"]["run_id"], "run_123")
        self.assertEqual(state["mlflow"]["run"]["run_id"], "run_123")
        self.assertIn("MLflow", state["mlflow_guidance"])

        http_state = self.request("GET", f"/api/projects/{project_id}/experiments/{exp_id}")
        self.assertTrue(http_state["mlflow"]["configured"])
        self.assertEqual(http_state["mlflow"]["experiment_name"], f"merv/{project_id}/{exp_id}")
        self.assertEqual(http_state["mlflow"]["run"]["run_id"], "run_123")

        # The standalone MLflow context tool returns the same block on demand.
        ctx = self.app.call_tool(
            "mlflow.context",
            {"project_id": project_id, "experiment_id": exp_id},
        )
        self.assertEqual(ctx["scope"], "experiment")
        self.assertTrue(ctx["mlflow"]["configured"])
        self.assertEqual(ctx["mlflow"]["experiment_name"], f"merv/{project_id}/{exp_id}")
        self.assertIn("MLFLOW_EXPERIMENT_NAME", ctx["mlflow"]["env"])
        self.assertEqual(ctx["mlflow"]["run"]["run_id"], "run_123")
        self.assertEqual(ctx["mlflow"]["env"]["MLFLOW_RUN_ID"], "run_123")

        finalized = {
            "configured": True,
            "control_configured": True,
            "experiment_name": f"merv/{project_id}/{exp_id}",
            "run_id": "run_123",
            "requested_status": "FINISHED",
            "update": {"attempted": True, "status": "FINISHED", "applied": True},
            "readback_attempts": 2,
            "terminal": True,
            "run": {
                "experiment_id": "7",
                "run_id": "run_123",
                "run_name": f"{exp_id}-attempt-1",
                "status": "FINISHED",
                "artifact_uri": "s3://mlflow/run_123",
                "created_at": "2026-07-02T12:00:00Z",
                "ended_at": "2026-07-02T12:05:00Z",
            },
        }
        with patch.object(CentralMlflowService, "finalize_run", return_value=finalized) as finalize_run:
            final = self.app.call_tool(
                "mlflow.finalize_run",
                {"project_id": project_id, "experiment_id": exp_id},
            )
        finalize_run.assert_called_once_with(
            project_id=project_id,
            experiment_id=exp_id,
            run_id="run_123",
            status="FINISHED",
            wait_seconds=2.0,
        )
        self.assertTrue(final["terminal"])
        self.assertEqual(final["run"]["status"], "FINISHED")
        self.assertEqual(final["experiment"]["mlflow_run"]["status"], "FINISHED")
        self.assertEqual(final["experiment"]["mlflow"]["run"]["status"], "FINISHED")

        # Without an experiment id it gives project-level navigation context.
        project_ctx = self.app.call_tool("mlflow.context", {"project_id": project_id})
        self.assertEqual(project_ctx["scope"], "project")
        self.assertEqual(project_ctx["mlflow"]["tracking_uri"], "https://mlflow.test")
        self.assertEqual(
            project_ctx["mlflow"]["experiment_namespace_prefix"], f"merv/{project_id}/"
        )
        self.assertEqual(
            project_ctx["mlflow"]["experiments"][0]["mlflow_experiment_name"],
            f"merv/{project_id}/{exp_id}",
        )
        self.assertNotIn("MLFLOW_EXPERIMENT_NAME", project_ctx["mlflow"]["env"])

    def test_transition_credential_audiences_and_telemetry_redaction(self) -> None:
        project_id = self.request(
            "POST", "/api/projects", {"name": "MLflow Credential Audiences"}
        )["id"]
        experiment_ids = [
            self.request(
                "POST",
                f"/api/projects/{project_id}/experiments",
                {"name": f"credential-{index}", "intent": "Characterize credentials"},
            )["id"]
            for index in range(3)
        ]
        self.configure_mlflow(
            CentralMlflowService(
                mode="external",
                tracking_uri="https://mlflow.test",
                server_uri="http://mlflow.internal:5000",
                agent_key="rr_sk_agent",
            )
        )
        self.app.mlflow_tracking.agent_key = "rr_sk_agent"
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id IN (?, ?)",
                tuple(experiment_ids[:2]),
            )
            conn.execute(
                "UPDATE experiments SET status = 'running' WHERE id = ?",
                (experiment_ids[2],),
            )

        with patch.object(
            self.app.transition_experiment,
            "execute",
            wraps=self.app.transition_experiment.execute,
        ) as execute, patch.object(
            CentralMlflowService,
            "create_run",
            return_value={"created": False, "configured": True},
        ):
            mcp_response = self.client.post(
                "/mcp/call",
                json={
                    "name": "experiment.transition",
                    "arguments": {
                        "project_id": project_id,
                        "experiment_id": experiment_ids[0],
                        "transition": "start_running",
                    },
                },
            )
            self.assertEqual(mcp_response.status_code, 200, mcp_response.text)
            mcp_result = mcp_response.json()["result"]
            rest_result = self.request(
                "POST",
                f"/api/projects/{project_id}/experiments/{experiment_ids[1]}/transition",
                {"transition": "start_running"},
            )

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(
            [call.kwargs["include_tracking_credentials"] for call in execute.call_args_list],
            [True, True],
        )

        for result in (mcp_result, rest_result):
            self.assertEqual(
                result["mlflow"]["env"]["MLFLOW_TRACKING_PASSWORD"],
                "rr_sk_agent",
            )

        ui_state = self.request(
            "GET", f"/api/projects/{project_id}/experiments/{experiment_ids[2]}"
        )
        self.assertNotIn("MLFLOW_TRACKING_PASSWORD", ui_state["mlflow"]["env"])

        activity_text = json.dumps(self.app.activity.recent(limit=100), sort_keys=True)
        tool_calls = self.app.tool_calls.stats(tool="experiment.transition")
        tool_call_details = [
            self.app.tool_calls.get(call_id=call["id"])
            for call in tool_calls["calls"]
        ]
        self.assertNotIn("rr_sk_agent", activity_text)
        self.assertNotIn("rr_sk_agent", json.dumps(tool_call_details, sort_keys=True))
        self.assertIn("[redacted]", activity_text)
        self.assertIn("[redacted]", json.dumps(tool_call_details, sort_keys=True))

    def test_application_facades_share_the_composed_service_instances(self) -> None:
        self.assertIs(self.app.research_core._experiments, self.app.experiments)
        self.assertIs(self.app.artifacts._submissions, self.app.artifact_submissions)
        self.assertTrue(callable(self.app.feed.transition_advisory))
        self.assertIs(self.app.transition_experiment.research, self.app.research_core)
        self.assertIs(self.app.transition_experiment.exhibits, self.app.experiment_exhibits)
        self.assertIs(self.app.tracking_context.research, self.app.research_core)
        self.assertIs(self.app.finalize_tracking_run.research, self.app.research_core)
        self.assertIs(
            self.app.tools._tools["mlflow.context"].handler.__self__,
            self.app.tracking_context,
        )
        self.assertIs(
            self.app.tools._tools["mlflow.finalize_run"].handler.__self__,
            self.app.finalize_tracking_run,
        )

    def test_attempt_revision_and_retry_rotate_the_mlflow_run(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "ML Rotate Project"})
        project_id = project["id"]
        exp_id = self.request(
            "POST", f"/api/projects/{project_id}/experiments", {"name": "exp-rotate", "intent": "Train"}
        )["id"]
        mlflow = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.test",
            server_uri="http://mlflow.internal:5000",
            dashboard_url="https://mlflow.test",
            health_check=lambda: True,
        )
        self.configure_mlflow(mlflow)
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,))

        def run_payload(run_id: str, attempt: int) -> dict:
            return {
                "created": True,
                "experiment_id": "7",
                "run_id": run_id,
                "run_name": f"{exp_id}-attempt-{attempt}",
                "status": "RUNNING",
                "artifact_uri": "",
                "created_at": "2026-07-02T12:00:00Z",
            }

        with patch.object(CentralMlflowService, "create_run", return_value=run_payload("run_a1", 1)):
            self.app.call_tool(
                "experiment.transition",
                {"project_id": project_id, "experiment_id": exp_id, "transition": "start_running"},
            )

        # An infra retry while the run is still open resumes the same run.
        with patch.object(CentralMlflowService, "create_run") as create_run:
            retried = self.app.call_tool(
                "experiment.transition",
                {"project_id": project_id, "experiment_id": exp_id, "transition": "retry_running"},
            )
        create_run.assert_not_called()
        self.assertEqual(retried["mlflow"]["run"]["run_id"], "run_a1")

        # Once that run was finalized, resuming would log into a closed run —
        # the retry mints a fresh run for the same attempt.
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET mlflow_run_status = 'FAILED' WHERE id = ?", (exp_id,))
        with patch.object(
            CentralMlflowService, "create_run", return_value=run_payload("run_a1_retry", 1)
        ) as create_run:
            retried = self.app.call_tool(
                "experiment.transition",
                {"project_id": project_id, "experiment_id": exp_id, "transition": "retry_running"},
            )
        create_run.assert_called_once()
        self.assertEqual(retried["mlflow"]["run"]["run_id"], "run_a1_retry")

        # A review rejection bumps the attempt and clears the run identity...
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'experiment_review' WHERE id = ?", (exp_id,))
            self.app.experiments.send_back_to_planned(
                conn=conn, experiment_id=exp_id, revision_context="revise the plan"
            )
        state = self.app.call_tool(
            "experiment.get_state", {"project_id": project_id, "experiment_id": exp_id}
        )
        self.assertFalse(state.get("mlflow_run"))
        # ...so the revised attempt's start mints an attempt-2 run instead of
        # handing back attempt 1's finalized one.
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,))
        with patch.object(
            CentralMlflowService, "create_run", return_value=run_payload("run_a2", 2)
        ) as create_run:
            transitioned = self.app.call_tool(
                "experiment.transition",
                {"project_id": project_id, "experiment_id": exp_id, "transition": "start_running"},
            )
        create_run.assert_called_once_with(
            project_id=project_id,
            experiment_id=exp_id,
            attempt_index=2,
            run_name=f"{exp_id}-attempt-2",
        )
        self.assertEqual(transitioned["mlflow"]["run"]["run_id"], "run_a2")

    def test_finalizing_a_foreign_run_id_keeps_the_canonical_run(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "ML Foreign Run Project"})
        project_id = project["id"]
        exp_id = self.request(
            "POST", f"/api/projects/{project_id}/experiments", {"name": "exp-foreign", "intent": "Train"}
        )["id"]
        mlflow = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.test",
            server_uri="http://mlflow.internal:5000",
            health_check=lambda: True,
        )
        self.configure_mlflow(mlflow)
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'running', mlflow_run_id = 'run_mine', "
                "mlflow_run_status = 'RUNNING' WHERE id = ?",
                (exp_id,),
            )
        finalized = {
            "configured": True,
            "terminal": True,
            "run": {"run_id": "run_other", "status": "FINISHED"},
        }
        with self.app.store.connect() as conn:
            refreshes_before = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE target_id = ? "
                    "AND type = 'experiment.mlflow_run_refreshed'",
                    (exp_id,),
                ).fetchone()["count"]
            )
        with patch.object(CentralMlflowService, "finalize_run", return_value=finalized):
            self.app.call_tool(
                "mlflow.finalize_run",
                {"project_id": project_id, "experiment_id": exp_id, "run_id": "run_other"},
            )
        state = self.app.call_tool(
            "experiment.get_state", {"project_id": project_id, "experiment_id": exp_id}
        )
        self.assertEqual(state["mlflow_run"]["run_id"], "run_mine")
        with self.app.store.connect() as conn:
            refreshes_after = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE target_id = ? "
                    "AND type = 'experiment.mlflow_run_refreshed'",
                    (exp_id,),
                ).fetchone()["count"]
            )
        self.assertEqual(refreshes_after, refreshes_before)

    def test_direct_and_mcp_finalize_share_response_and_ledger_semantics(self) -> None:
        project_id = self.request(
            "POST", "/api/projects", {"name": "MLflow Finalize Delivery"}
        )["id"]
        experiment_ids = [
            self.request(
                "POST",
                f"/api/projects/{project_id}/experiments",
                {"name": f"finalize-{index}", "intent": "Compare delivery"},
            )["id"]
            for index in range(2)
        ]
        self.configure_mlflow(
            CentralMlflowService(
                mode="external",
                tracking_uri="https://mlflow.test",
                server_uri="http://mlflow.internal:5000",
            )
        )
        with self.app.store.transaction() as conn:
            for index, experiment_id in enumerate(experiment_ids):
                conn.execute(
                    "UPDATE experiments SET status = 'running', mlflow_run_id = ?, "
                    "mlflow_run_status = 'RUNNING' WHERE id = ?",
                    (f"run_{index}", experiment_id),
                )

        def finalized(**kwargs):
            return {
                "configured": True,
                "terminal": True,
                "run": {"run_id": kwargs["run_id"], "status": "FINISHED"},
            }

        def cursor() -> int:
            with self.app.store.connect() as conn:
                return int(
                    conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM events")
                    .fetchone()["id"]
                )

        with patch.object(CentralMlflowService, "finalize_run", side_effect=finalized):
            first_cursor = cursor()
            direct = self.app.call_tool(
                "mlflow.finalize_run",
                {"project_id": project_id, "experiment_id": experiment_ids[0]},
            )
            second_cursor = cursor()
            response = self.client.post(
                "/mcp/call",
                json={
                    "name": "mlflow.finalize_run",
                    "arguments": {
                        "project_id": project_id,
                        "experiment_id": experiment_ids[1],
                    },
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            mcp = response.json()["result"]

        def normalized(result):
            value = deepcopy(result)
            value["experiment_id"] = "exp"
            value["run"]["run_id"] = "run"
            value["experiment"]["id"] = "exp"
            value["experiment"]["name"] = "name"
            value["experiment"]["created_at"] = "time"
            value["experiment"]["updated_at"] = "time"
            value["experiment"]["mlflow_run"]["run_id"] = "run"
            value["experiment"]["mlflow_run"]["created_at"] = "time"
            value["experiment"]["mlflow"]["run"]["run_id"] = "run"
            value["experiment"]["mlflow"]["run"]["created_at"] = "time"
            value["experiment"]["mlflow"]["experiment_name"] = "tracking-name"
            value["experiment"]["mlflow"]["env"]["MLFLOW_EXPERIMENT_NAME"] = "tracking-name"
            value["experiment"]["mlflow"]["env"]["RP_EXPERIMENT_ID"] = "exp"
            value["experiment"]["mlflow"]["env"]["MLFLOW_RUN_ID"] = "run"
            value["experiment"]["mlflow"]["env"]["RP_MLFLOW_RUN_ID"] = "run"
            value["feed_note"] = "note"
            return value

        self.assertEqual(
            normalized(direct),
            normalized(mcp),
        )
        with self.app.store.connect() as conn:
            direct_rows = conn.execute(
                "SELECT type, target_id FROM events WHERE id > ? AND id <= ? ORDER BY id",
                (first_cursor, second_cursor),
            ).fetchall()
            mcp_rows = conn.execute(
                "SELECT type, target_id FROM events WHERE id > ? ORDER BY id",
                (second_cursor,),
            ).fetchall()
        self.assertEqual(
            [(row["type"], row["target_id"]) for row in direct_rows],
            [("experiment.mlflow_run_refreshed", experiment_ids[0])],
        )
        self.assertEqual(
            [(row["type"], row["target_id"]) for row in mcp_rows],
            [("experiment.mlflow_run_refreshed", experiment_ids[1])],
        )

    def test_server_only_mlflow_does_not_advertise_agent_tracking(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "ML Server Project"})
        project_id = project["id"]
        exp = self.request(
            "POST",
            f"/api/projects/{project_id}/experiments",
            {"name": "exp-server", "intent": "Train"},
        )
        exp_id = exp["id"]
        self.configure_mlflow(
            CentralMlflowService(mode="external", server_uri="http://mlflow:5000")
        )

        ctx = self.app.call_tool(
            "mlflow.context",
            {"project_id": project_id, "experiment_id": exp_id},
        )

        self.assertFalse(ctx["mlflow"]["configured"])
        self.assertNotIn("MLFLOW_TRACKING_URI", ctx["mlflow"]["env"])
        self.assertIn("agents cannot log or browse", ctx["guidance"])
        self.assertIn("MERV_MLFLOW_TRACKING_URI", ctx["guidance"])

    def test_home_exposes_active_experiments_and_processes(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Active Work Project"})
        project_id = project["id"]
        planned = self.request("POST", f"/api/projects/{project_id}/experiments", {"name": "exp-4", "intent": "Planned active work"})
        running = self.request("POST", f"/api/projects/{project_id}/experiments", {"name": "exp-5", "intent": "Running active work"})
        complete = self.request("POST", f"/api/projects/{project_id}/experiments", {"name": "exp-6", "intent": "Finished work"})
        now = now_iso()
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'running', updated_at = ? WHERE id = ?", (now, running["id"]))
            conn.execute("UPDATE experiments SET status = 'complete', updated_at = ? WHERE id = ?", (now, complete["id"]))
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, project_id, sandbox_id, status,
                  created_at, updated_at
                )
                VALUES ('uid_active', ?, 'sb_active', 'running', ?, ?)
                """,
                (project_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at
                )
                VALUES ('uid_active', ?, ?)
                """,
                (running["id"], now),
            )
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, project_id, sandbox_id, status,
                  terminated_at, created_at, updated_at
                )
                VALUES ('uid_done', ?, 'sb_done', 'terminated', ?, ?, ?)
                """,
                (project_id, now, now, now),
            )

        home = self.request("GET", f"/api/projects/{project_id}/home")

        self.assertEqual([item["id"] for item in home["active_experiments"]], [running["id"], planned["id"]])
        self.assertEqual(home["active_experiment"]["id"], running["id"])
        self.assertEqual(home["workflow"]["next_action"], "run_experiment_and_retain_results")
        self.assertEqual(home["stats"]["active_experiments"], 2)
        self.assertEqual(home["stats"]["active_processes"], 1)
        self.assertEqual(home["active_processes"][0]["experiment_id"], running["id"])
        self.assertEqual(home["active_processes"][0]["process_type"], "sandbox")
        self.assertEqual(home["active_processes"][0]["experiment"]["id"], running["id"])
        self.assertNotIn(complete["id"], [item["id"] for item in home["active_experiments"]])

    def test_activity_endpoint_reports_recent_tool_calls(self) -> None:
        self.request("GET", "/api/projects")
        activity = self.request("GET", "/api/activity?limit=5")
        self.assertNotIn("activity_log", activity)
        self.assertTrue(
            any(
                event.get("event") == "tool.call"
                and event.get("source") == "http"
                and event.get("tool") == "project.list"
                and "projects" in event.get("result", {})
                for event in activity["events"]
            )
        )

    def test_experiment_figure_endpoint(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Figure Project"})
        pid = project["id"]
        claim = self.request("POST", f"/api/projects/{pid}/claims", {"statement": "Rank-8 LoRA matches full FT."})
        exp = self.request(
            "POST",
            f"/api/projects/{pid}/experiments",
            {"name": "lora-ranks", "intent": "Compare LoRA ranks.", "claim_ids": [claim["id"]]},
        )
        exp_id = exp["id"]
        plan = self.submit(
            pid=pid,
            target_type="experiment",
            target_id=exp_id,
            role="plan",
            path="plan.md",
            body=(
                "## Summary\nCompare LoRA ranks.\n\n"
                "## Objective & hypothesis\nRank 8 suffices.\n\n"
                "## Evaluation\nMetric: eval loss delta; success if within 0.05.\n"
            ),
        )
        plan_id = plan["artifact_id"]

        figure = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/figure")
        nodes = {node["id"]: node for node in figure["nodes"]}
        edge_ids = {edge["id"] for edge in figure["edges"]}
        self.assertEqual(figure["source"], "derived")
        self.assertEqual(figure["attempt_index"], 1)
        self.assertEqual(nodes["attempt:1"]["status"], "pending")
        self.assertEqual(nodes[f"artifact:{plan_id}:a1"]["sublabel"], "plan")
        self.assertIn(f"artifact:{plan_id}:a1->attempt:1:feeds", edge_ids)
        self.assertEqual(nodes[f"claim:{claim['id']}"]["type"], "claim")
        self.assertIn(f"attempt:1->claim:{claim['id']}:tests", edge_ids)

        # Design-review round: the open gate appears, then needs_changes draws the revision loop.
        self.request("POST", f"/api/projects/{pid}/experiments/{exp_id}/transition", {"transition": "submit_design"})
        req = self.request(
            "POST",
            f"/api/projects/{pid}/reviews/request",
            {"target_type": "experiment", "target_id": exp_id, "role": "design_reviewer"},
        )
        figure = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/figure")
        nodes = {node["id"]: node for node in figure["nodes"]}
        self.assertEqual(nodes[f"review_request:{req['review_request_id']}"]["status"], "open")

        session = self.request(
            "POST",
            f"/api/projects/{pid}/reviews/start",
            {
                "review_request_id": req["review_request_id"],
                "reviewer_capability": req["reviewer_capability"],
                "caller_session_id": "rev",
            },
        )
        self.request(
            "POST",
            f"/api/projects/{pid}/reviews/submit",
            {
                "review_session_id": session["review_session_id"],
                "verdict": "needs_changes",
                "synopsis": "The design does not yet test the claim as scoped, so it needs revision.",
            },
        )

        figure = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/figure")
        nodes = {node["id"]: node for node in figure["nodes"]}
        edge_ids = {edge["id"] for edge in figure["edges"]}
        self.assertEqual(figure["attempt_index"], 2)
        self.assertEqual(nodes["attempt:1"]["status"], "superseded")
        self.assertEqual(nodes["attempt:2"]["status"], "pending")
        self.assertIn("attempt:1->attempt:2:revised_to", edge_ids)
        self.assertNotIn(f"review_request:{req['review_request_id']}", nodes)
        review_nodes = [n for n in figure["nodes"] if n["type"] == "review" and n["status"] == "needs_changes"]
        self.assertEqual(len(review_nodes), 1)
        self.assertEqual(review_nodes[0]["group"], "attempt:1")
        self.assertIn(f"{review_nodes[0]['id']}->attempt:2:revised_to", edge_ids)

        # Sandbox liveness and the conclusion both surface as derived nodes.
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,))
        self.app.call_tool("sandbox.request", {"project_id": pid, "experiment_id": exp_id, "gpu": "A100"})
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'complete', conclusion = 'Rank 8 is enough.' WHERE id = ?",
                (exp_id,),
            )

        figure = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/figure")
        nodes = {node["id"]: node for node in figure["nodes"]}
        edge_ids = {edge["id"] for edge in figure["edges"]}
        self.assertEqual(nodes["attempt:2"]["status"], "done")
        self.assertEqual(nodes["sandbox"]["status"], "active")
        self.assertIn("attempt:2->sandbox:ran_on", edge_ids)
        self.assertEqual(nodes["conclusion"]["sublabel"], "Rank 8 is enough.")
        self.assertIn("attempt:2->conclusion:concludes", edge_ids)
        self.assertIn(f"conclusion->claim:{claim['id']}:tests", edge_ids)

        missing = self.client.request("GET", f"/api/projects/{pid}/experiments/exp_nope/figure")
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_experiment_logic_graph_endpoint_resolves_refs(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Graph Project"})
        pid = project["id"]
        claim = self.request("POST", f"/api/projects/{pid}/claims", {"statement": "Warmup matters."})
        exp = self.request(
            "POST",
            f"/api/projects/{pid}/experiments",
            {"name": "warmup-sensitivity", "intent": "Test warmup sensitivity.", "claim_ids": [claim["id"]]},
        )
        exp_id = exp["id"]

        # No graph associated yet — the endpoint reports that plainly.
        empty = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/graph")
        self.assertFalse(empty["available"])
        self.assertEqual(empty["max_nodes"], 16)

        # A passing design review supplies a real rev_ id to reference.
        plan = self.submit(
            pid=pid,
            target_type="experiment",
            target_id=exp_id,
            role="plan",
            path="plan.md",
            body=(
                "## Summary\nWarmup sweep.\n\n"
                "## Objective & hypothesis\nWarmup changes accuracy.\n\n"
                "## Evaluation\nMetric: accuracy delta; success if > 1pt.\n"
            ),
        )
        self.request("POST", f"/api/projects/{pid}/experiments/{exp_id}/transition", {"transition": "submit_design"})
        req = self.request(
            "POST",
            f"/api/projects/{pid}/reviews/request",
            {"target_type": "experiment", "target_id": exp_id, "role": "design_reviewer"},
        )
        session = self.request(
            "POST",
            f"/api/projects/{pid}/reviews/start",
            {
                "review_request_id": req["review_request_id"],
                "reviewer_capability": req["reviewer_capability"],
                "caller_session_id": "rev",
            },
        )
        review = self.request(
            "POST",
            f"/api/projects/{pid}/reviews/submit",
            {
                "review_session_id": session["review_session_id"],
                "verdict": "pass",
                "synopsis": "The plan and results check out, so the attempt stands as reported.",
            },
        )

        result = self.submit(
            pid=pid,
            target_type="experiment",
            target_id=exp_id,
            role="result",
            path="results.json",
            body='{"accuracy": 0.93}\n',
        )
        graph_body = {
            "version": 1,
            # Agent-authored fields are opaque data, even when a key matches a
            # local control-plane redaction name.
            "repo_root": "keep-in-authored-graph",
            "nodes": [
                {"id": "obj", "kind": "objective", "label": "Warmup sweep",
                 "refs": [claim["id"], exp_id]},
                {"id": "rev", "kind": "pivot", "label": "Design review passed",
                 "refs": [review["id"]]},
                {"id": "out", "kind": "outcome", "label": "Accuracy 93%",
                 "refs": [
                     result["artifact_id"],
                     plan["artifact_id"],
                     "art_missing",
                     "notes.md",
                 ]},
            ],
            "edges": [{"from": "obj", "to": "rev"}, {"from": "rev", "to": "out"}],
        }
        import json as _json

        self.submit(
            pid=pid,
            target_type="experiment",
            target_id=exp_id,
            role="graph",
            path="graph.json",
            body=_json.dumps(graph_body),
        )

        payload = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["problems"], [])
        self.assertEqual(len(payload["graph"]["nodes"]), 3)
        self.assertEqual(payload["graph"]["repo_root"], "keep-in-authored-graph")
        refs = payload["ref_index"]
        # art_ ids → submitted-artifact links.
        self.assertEqual(refs[result["artifact_id"]]["type"], "artifact")
        self.assertTrue(refs[result["artifact_id"]]["resolved"])
        self.assertEqual(refs[result["artifact_id"]]["path"], "results.json")
        self.assertEqual(refs[plan["artifact_id"]]["type"], "artifact")
        self.assertEqual(refs[plan["artifact_id"]]["path"], "plan.md")
        # rev_ / claim_ / exp_ ids → their records.
        self.assertEqual(refs[review["id"]]["type"], "review")
        self.assertEqual(refs[review["id"]]["verdict"], "pass")
        self.assertEqual(refs[claim["id"]]["type"], "claim")
        self.assertEqual(refs[claim["id"]]["statement"], "Warmup matters.")
        self.assertEqual(refs[exp_id]["type"], "experiment")
        # Unknown art_ ids and raw paths are unresolved with submit guidance —
        # refs resolve against records only, never a disk probe.
        for unresolved in ("art_missing", "notes.md"):
            self.assertFalse(refs[unresolved]["resolved"])
            self.assertIn("not a submitted artifact id", refs[unresolved]["hint"])

    def test_experiment_logic_graph_picks_latest_association_and_reports_broken_json(self) -> None:
        import json as _json

        project = self.request("POST", "/api/projects", {"name": "Graph Pick Project"})
        pid = project["id"]
        exp = self.request(
            "POST",
            f"/api/projects/{pid}/experiments",
            {"name": "graph-pick", "intent": "Pick the right graph file."},
        )
        exp_id = exp["id"]

        def graph_with(label):
            return _json.dumps({"version": 1, "nodes": [{"id": "n", "label": label}]})

        # Submit two graph-role files in the same attempt. The alphabetically
        # later path goes FIRST, so a last-by-path picker would choose it; the
        # endpoint must instead pick the most recently submitted file — the
        # same row the submit_results validator lints.
        for path, label in (("b_old.json", "old story"), ("a_new.json", "new story")):
            self.submit(
                pid=pid,
                target_type="experiment",
                target_id=exp_id,
                role="graph",
                path=path,
                body=graph_with(label),
            )

        payload = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["path"], "a_new.json")
        self.assertEqual(payload["graph"]["nodes"][0]["label"], "new story")

        # Resubmitting corrupted content replaces the pinned bytes: still
        # available (a graph exists), problems stated — the UI renders them
        # instead of hiding.
        self.submit(
            pid=pid,
            target_type="experiment",
            target_id=exp_id,
            role="graph",
            path="a_new.json",
            body="{not json",
        )
        payload = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/graph")
        self.assertTrue(payload["available"])
        self.assertIsNone(payload["graph"])
        self.assertTrue(any("not valid JSON" in p for p in payload["problems"]))

    def test_reflection_endpoints_and_project_graph(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Reflect"})
        pid = project["id"]

        # Drive a wave to published via the tool surface (the same path the
        # MCP proxy exercises); read everything back over HTTP.
        lenses = [
            {"id": "amplify"},
            {"id": "avoid"},
            {"id": "entropy"},
            {
                "id": "rigor",
                "charter": "Method soundness.",
                "why_distinct": "How we measured, not what we found.",
            },
            {
                "id": "cost",
                "charter": "Compute vs information gained.",
                "why_distinct": "Prices the exploration.",
            },
        ]
        syn = self.app.call_tool(
            "reflection.create",
            {"project_id": pid, "title": "Wave 1", "lenses": lenses},
        )
        syn_id = syn["id"]

        listing = self.request("GET", f"/api/projects/{pid}/reflections")
        self.assertEqual(len(listing["reflections"]), 1)
        self.assertEqual(listing["open_reflection"]["id"], syn_id)
        self.assertIn("signal", listing)
        checklist_item = listing["reflections"][0]["gate_checklist"]["items"][0]
        self.assertEqual(checklist_item["action"], "fan_out_reflection_subagents")
        self.assertLess(
            list(checklist_item).index("lens_id"),
            list(checklist_item).index("label"),
        )
        self.assertEqual(
            self.request("GET", f"/api/projects/{pid}/reflections/{syn_id}")[
                "gate_checklist"
            ]["items"][0]["action"],
            "fan_out_reflection_subagents",
        )

        # No graph yet: the project-graph endpoint degrades, not errors.
        empty = self.request("GET", f"/api/projects/{pid}/reflections/current/graph")
        self.assertFalse(empty["available"])

        avoid_lens_artifact_id = ""
        for lens in ("amplify", "avoid", "entropy", "rigor", "cost"):
            submitted = self.submit(
                pid=pid,
                target_type="reflection",
                target_id=syn_id,
                role="reflection_lens_doc",
                path=f"reflections/{syn_id}/reflections/{lens}.md",
                body=f"{lens} findings\n",
                lens_id=lens,
            )
            if lens == "avoid":
                avoid_lens_artifact_id = submitted["artifact_id"]
        self.app.call_tool(
            "reflection.transition",
            {"project_id": pid, "reflection_id": syn_id, "transition": "submit_reflections"},
        )

        # The project graph refs the wave itself (syn_) and a lens artifact.
        graph_text = (
            '{"version": 1, "title": "Project logic", "nodes": ['
            '{"id": "a", "kind": "lesson", "label": "Lesson", "refs": ["' + syn_id + '"]},'
            '{"id": "b", "kind": "open", "label": "Open question", '
            '"refs": ["' + avoid_lens_artifact_id + '"]}],'
            ' "edges": [{"from": "a", "to": "b"}]}'
        )
        reflection_text = (
            "# Reflection\n\n"
            "## Summary\nHTTP reflection test wave.\n\n"
            "![project graph](figures/project_graph.png)\n\n"
            "## Critical reading\nThe test wave adds one claim and two planned experiments.\n\n"
            "## Decision / future directions\nCreate both HTTP experiments in parallel.\n"
        )
        change_spec_text = (
            json.dumps(
                {
                    "version": 1,
                    "claim_changes": [
                        {
                            "op": "create",
                            "key": "claim_http_wave",
                            "statement": "HTTP reflection wave claim.",
                            "confidence": "medium",
                            "rationale": "The HTTP reflection test needs a materializable claim.",
                        }
                    ],
                    "decision": {
                        "type": "create_experiments",
                        "experiments": [
                            {
                                "key": "http_a",
                                "name": "http-wave-a",
                                "intent": "HTTP-created experiment A.",
                                "tested_claim_refs": ["claim_http_wave"],
                                "parallelism": "Independent HTTP test axis A.",
                            },
                            {
                                "key": "http_b",
                                "name": "http-wave-b",
                                "intent": "HTTP-created experiment B.",
                                "tested_claim_refs": ["claim_http_wave"],
                                "parallelism": "Independent HTTP test axis B.",
                            },
                        ],
                    },
                }
            )
        )
        self.submit(
            pid=pid,
            target_type="reflection",
            target_id=syn_id,
            role="project_graph",
            path="project/logic_graph.json",
            body=graph_text,
        )
        reflection_doc = self.submit(
            pid=pid,
            target_type="reflection",
            target_id=syn_id,
            role="reflection_doc",
            path="project/reflection.md",
            body=reflection_text,
        )
        # The upload response mints one figure token per markdown image link;
        # pushing the bytes completes the figure the same way the agent's
        # follow-up curl would.
        self.assertEqual(
            [fig["link_path"] for fig in reflection_doc["figures"]],
            ["figures/project_graph.png"],
        )
        self.app.upload_artifact_bytes(
            token=upload_token(reflection_doc["figures"][0]["run"]),
            data=b"\x89PNG\r\n\x1a\nfake",
            kind="f",
        )
        self.submit(
            pid=pid,
            target_type="reflection",
            target_id=syn_id,
            role="change_spec",
            path="project/change_spec.json",
            body=change_spec_text,
        )
        figure = self.client.get(
            f"/api/projects/{pid}/artifacts/{reflection_doc['artifact_id']}/figure",
            params={"rel": "figures/project_graph.png"},
        )
        self.assertEqual(figure.status_code, 200)
        self.assertTrue(figure.content.startswith(b"\x89PNG"))
        self.app.call_tool(
            "reflection.transition",
            {"project_id": pid, "reflection_id": syn_id, "transition": "submit_reflection_artifacts"},
        )

        # The open wave's graph renders while still under review.
        payload = self.request("GET", f"/api/projects/{pid}/reflections/current/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["reflection"]["id"], syn_id)
        self.assertEqual(payload["reflection"]["status"], "reflection_review")
        self.assertEqual(payload["problems"], [])
        refs = payload["ref_index"]
        self.assertEqual(refs[syn_id]["type"], "reflection")
        self.assertTrue(refs[syn_id]["resolved"])
        self.assertEqual(refs[syn_id]["title"], "Wave 1")
        reflection_ref = refs[avoid_lens_artifact_id]
        self.assertEqual(reflection_ref["type"], "artifact")

        # Review over the HTTP review endpoints (target-polymorphic).
        req = self.request(
            "POST",
            f"/api/projects/{pid}/reviews/request",
            {"target_type": "reflection", "target_id": syn_id, "role": "reflection_reviewer"},
        )
        session = self.request(
            "POST",
            f"/api/projects/{pid}/reviews/start",
            {
                "review_request_id": req["review_request_id"],
                "reviewer_capability": req["reviewer_capability"],
                "caller_session_id": "http-reviewer",
            },
        )
        self.request(
            "POST",
            f"/api/projects/{pid}/reviews/submit",
            {
                "review_session_id": session["review_session_id"],
                "verdict": "pass",
                "synopsis": "The reflection wave honestly represents the project's logic state.",
            },
        )
        self.app.call_tool(
            "reflection.transition",
            {"project_id": pid, "reflection_id": syn_id, "transition": "publish"},
        )

        detail = self.request("GET", f"/api/projects/{pid}/reflections/{syn_id}")
        self.assertEqual(detail["status"], "published")
        self.assertTrue(detail["published_graph_version_id"])
        self.assertEqual(len(detail["roster"]), 5)

        # Published wave still serves the living graph as "current".
        payload = self.request("GET", f"/api/projects/{pid}/reflections/current/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["reflection"]["status"], "published")
        listing = self.request("GET", f"/api/projects/{pid}/reflections")
        self.assertIsNone(listing["open_reflection"])
        self.assertEqual(listing["latest_published"]["id"], syn_id)
        self.assertEqual(listing["current"]["id"], syn_id)

    def test_per_wave_graph_and_pinned_artifact_content(self) -> None:
        # The reflection-wave UI renders a SPECIFIC wave's graph + content from
        # the artifacts that wave pinned, so it stays faithful after later
        # waves submit new versions of the living files.
        roster = [
            {"id": "amplify"},
            {"id": "avoid"},
            {"id": "entropy"},
            {"id": "rigor", "charter": "Method soundness.", "why_distinct": "How, not what."},
            {"id": "cost", "charter": "Compute spent.", "why_distinct": "Prices exploration."},
        ]
        project = self.request("POST", "/api/projects", {"name": "Pin"})
        pid = project["id"]
        wave1_id = self.app.call_tool(
            "reflection.create", {"project_id": pid, "title": "Wave 1", "lenses": roster}
        )["id"]

        # Before any graph is submitted, the per-wave endpoint degrades cleanly.
        empty = self.request("GET", f"/api/projects/{pid}/reflections/{wave1_id}/graph")
        self.assertFalse(empty["available"])

        graph_text = (
            '{"version": 1, "title": "Wave 1 logic", "nodes": ['
            '{"id": "a", "kind": "lesson", "label": "A lesson"}], "edges": []}'
        )
        wave1_graph = self.submit(
            pid=pid,
            target_type="reflection",
            target_id=wave1_id,
            role="project_graph",
            path="project/logic_graph.json",
            body=graph_text,
        )

        # The per-wave graph renders the wave's pinned bytes.
        payload = self.request("GET", f"/api/projects/{pid}/reflections/{wave1_id}/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["reflection"]["id"], wave1_id)
        self.assertEqual(payload["graph"]["nodes"][0]["id"], "a")
        self.assertEqual(payload["problems"], [])

        # The wave detail exposes the pinned artifact id.
        detail = self.request("GET", f"/api/projects/{pid}/reflections/{wave1_id}")
        graph_row = next(
            r for r in detail["artifacts"] if r["role"] == "project_graph"
        )
        self.assertEqual(graph_row["id"], wave1_graph["artifact_id"])

        # The artifact content endpoint serves the exact submitted bytes.
        pinned = self.request(
            "GET", f"/api/projects/{pid}/artifacts/{wave1_graph['artifact_id']}/content"
        )
        self.assertEqual(pinned["content"], graph_text)
        self.assertEqual(pinned["source"], "submitted")

        # An unknown artifact id is rejected, not served.
        bad = self.client.get(f"/api/projects/{pid}/artifacts/art_bogus/content")
        self.assertEqual(bad.status_code, 404)

        # The literal current/graph route still resolves (not captured by the
        # {reflection_id}/graph param route).
        current = self.request("GET", f"/api/projects/{pid}/reflections/current/graph")
        self.assertTrue(current["available"])
        self.assertEqual(current["reflection"]["id"], wave1_id)

        # A second wave submits its own version of the living file; wave 1's
        # artifact keeps serving its original pinned bytes.
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE reflections SET status = 'published' WHERE id = ?", (wave1_id,)
            )
        wave2_id = self.app.call_tool(
            "reflection.create", {"project_id": pid, "title": "Wave 2", "lenses": roster}
        )["id"]
        new_text = graph_text.replace("Wave 1 logic", "Wave 2 logic")
        wave2_graph = self.submit(
            pid=pid,
            target_type="reflection",
            target_id=wave2_id,
            role="project_graph",
            path="project/logic_graph.json",
            body=new_text,
        )
        self.assertNotEqual(wave2_graph["artifact_id"], wave1_graph["artifact_id"])
        self.assertEqual(
            self.request(
                "GET", f"/api/projects/{pid}/artifacts/{wave2_graph['artifact_id']}/content"
            )["content"],
            new_text,
        )
        self.assertEqual(
            self.request(
                "GET", f"/api/projects/{pid}/artifacts/{wave1_graph['artifact_id']}/content"
            )["content"],
            graph_text,
        )


class ArtifactFigureRouteTest(unittest.TestCase):
    """GET /artifacts/{id}/figure?rel=... serves submitted figure bytes only."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(create_fastapi_app(self.app.http))
        project = self.client.post("/api/projects", json={"name": "Rel"}).json()
        self.project_id = project["id"]
        exp = self.client.post(
            f"/api/projects/{self.project_id}/experiments",
            json={"name": "plan-figure", "intent": "Verify submitted plan image."},
        ).json()
        self.plan = self.app.submit_artifact(
            project_id=self.project_id,
            target_type="experiment",
            target_id=exp["id"],
            role="plan",
            path="plans/plan.md",
            body=(
                "## Summary\nPlan with a diagram.\n\n"
                "![diagram](figures/diagram.png)\n\n"
                "## Objective & hypothesis\nTest plan image serving.\n\n"
                "## Evaluation\nSuccess means the backend serves submitted bytes.\n"
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unsubmitted_figure_is_not_found(self) -> None:
        # The token was minted but the figure bytes were never pushed.
        response = self.client.get(
            f"/api/projects/{self.project_id}/artifacts/{self.plan['artifact_id']}/figure",
            params={"rel": "figures/diagram.png"},
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_serves_submitted_plan_figure_without_live_file(self) -> None:
        self.assertEqual(
            [fig["link_path"] for fig in self.plan["figures"]],
            ["figures/diagram.png"],
        )
        self.app.upload_artifact_bytes(
            token=upload_token(self.plan["figures"][0]["run"]),
            data=b"\x89PNG\r\n\x1a\nplan",
            kind="f",
        )
        response = self.client.get(
            f"/api/projects/{self.project_id}/artifacts/{self.plan['artifact_id']}/figure",
            params={"rel": "figures/diagram.png"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\x89PNG\r\n\x1a\nplan")

    def test_unknown_rel_is_not_found(self) -> None:
        response = self.client.get(
            f"/api/projects/{self.project_id}/artifacts/{self.plan['artifact_id']}/figure",
            params={"rel": "figures/nope.png"},
        )
        self.assertEqual(response.status_code, 404)


class FigureViewTest(unittest.TestCase):
    def test_artifact_fanout_rolls_up_past_cap(self) -> None:
        from merv.brain.application.experiment_figure import (
            ARTIFACT_FANOUT_CAP,
            build_experiment_figure,
        )

        experiment = {
            "id": "exp_x",
            "intent": "Stress the figure.",
            "status": "running",
            "attempt_index": 1,
            "conclusion": "",
            "tested_claims": [],
            "reviews": [],
            "artifacts": [
                {
                    "id": f"art_{i:03d}",
                    "path": f"results/file_{i:03d}.json",
                    "title": "",
                    "role": "result",
                    "attempt_index": 1,
                }
                for i in range(20)
            ],
        }
        figure = build_experiment_figure(
            experiment=experiment,
            review_attempts={},
            open_review_requests=[],
            sandbox=None,
        )
        artifact_nodes = [n for n in figure["nodes"] if n["type"] == "artifact"]
        group_nodes = [n for n in figure["nodes"] if n["type"] == "artifact_group"]
        self.assertEqual(len(artifact_nodes), ARTIFACT_FANOUT_CAP)
        self.assertEqual(len(group_nodes), 1)
        self.assertEqual(group_nodes[0]["meta"]["count"], 20 - ARTIFACT_FANOUT_CAP)
        self.assertIn("attempt:1->artifact_group:a1:down:produced", {e["id"] for e in figure["edges"]})
        # Live attempt status flows through to the spine node.
        attempt = next(n for n in figure["nodes"] if n["id"] == "attempt:1")
        self.assertEqual(attempt["status"], "active")


class DegradedStatesTest(unittest.TestCase):
    """Artifact content reads return documented degraded shapes, not 500s."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(create_fastapi_app(self.app.http), raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pending_artifact_content_is_unavailable_not_500(self) -> None:
        project = self.client.post(
            "/api/projects", json={"name": "Proj P", "summary": "s"}
        ).json()
        pid = project["id"]
        exp = self.client.post(
            f"/api/projects/{pid}/experiments",
            json={"name": "deg-exp", "intent": "Degraded read."},
        ).json()
        # Submitted but never uploaded: the row is pending, no bytes exist.
        pending = self.app.call_tool(
            "artifact.submit",
            {
                "project_id": pid,
                "target_type": "experiment",
                "target_id": exp["id"],
                "role": "result",
                "path": "results.json",
            },
        )
        body = self.client.get(
            f"/api/projects/{pid}/artifacts/{pending['artifact_id']}/content"
        ).json()
        self.assertFalse(body["available"])
        self.assertIsNone(body["content"])
        self.assertEqual(body["source"], "unavailable")
        # The raw-file route reports not-found instead of erroring.
        raw = self.client.get(
            f"/api/projects/{pid}/artifacts/{pending['artifact_id']}/file"
        )
        self.assertEqual(raw.status_code, 404)
