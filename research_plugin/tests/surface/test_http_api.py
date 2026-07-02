from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import ResearchPluginApp
from backend.mlflow import CentralMlflowService
from backend.transport.http_api import ResearchHttpApi, create_fastapi_app
from backend.daemon.project_router import ProjectRouter
from backend.execution.backends.fake import FakeSandboxBackend
from backend.utils import ContentUnavailableError
from mcp_server.time_utils import now_iso


class ResearchPluginHttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.client = TestClient(create_fastapi_app(self.app))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        response = self.client.request(method, path, json=body)
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

    def configure_mlflow(self, service: CentralMlflowService) -> None:
        self.app.mlflow_tracking.mode = service.mode
        self.app.mlflow_tracking.tracking_uri = service.tracking_uri
        self.app.mlflow_tracking._control_uri = service._control_uri
        self.app.mlflow_tracking.server_uri = service.server_uri
        self.app.mlflow_tracking.dashboard_url = service.dashboard_url
        self.app.mlflow_tracking.note = service.note
        self.app.mlflow_tracking._health_check = service._health_check

    def test_home_claim_experiment_resource_review_endpoints(self) -> None:
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
        (self.repo / "plan.md").write_text(
            "## Summary\nCompare a threshold classifier with the baseline.\n\n"
            "## Objective & hypothesis\nThreshold rule beats majority class.\n\n"
            "## Evaluation\nMetric: accuracy vs majority baseline; success if higher.\n"
        )
        resource = self.request(
            "POST",
            f"/api/projects/{project_id}/resources",
            {"path": "plan.md", "kind": "note", "title": "Plan"},
        )
        self.request(
            "POST",
            f"/api/projects/{project_id}/resources/{resource['id']}/associate",
            {"target_type": "experiment", "target_id": exp_id, "role": "plan"},
        )

        home = self.request("GET", f"/api/projects/{project_id}/home")
        self.assertEqual(home["project"]["name"], "UI Project")
        self.assertEqual(home["stats"]["claims"], 1)
        self.assertEqual(home["workflow"]["next_action"], "submit_design_for_review")

        content = self.request("GET", f"/api/projects/{project_id}/resources/{resource['id']}/content")
        self.assertIn("accuracy", content["content"])
        history = self.request("GET", f"/api/projects/{project_id}/resources/{resource['id']}/history")
        version_id = history["versions"][0]["id"]
        self.assertTrue(version_id)
        deleted_resource = self.request("DELETE", f"/api/projects/{project_id}/resources/{resource['id']}")
        self.assertTrue(deleted_resource["deleted"])
        self.assertEqual(self.request("GET", f"/api/projects/{project_id}/resources")["resources"], [])
        resource = self.request(
            "POST",
            f"/api/projects/{project_id}/resources",
            {"path": "plan.md", "kind": "note", "title": "Plan"},
        )
        self.assertEqual(resource["id"], deleted_resource["resource"]["id"])
        self.request(
            "POST",
            f"/api/projects/{project_id}/resources/{resource['id']}/associate",
            {"target_type": "experiment", "target_id": exp_id, "role": "plan"},
        )

        self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/transition", {"transition": "submit_design"})
        review_request = self.request(
            "POST",
            f"/api/projects/{project_id}/reviews/request",
            {"target_type": "experiment", "target_id": exp_id, "role": "design_reviewer"},
        )
        self.assertEqual(review_request["role"], "design_reviewer")
        self.assertEqual(review_request["target_snapshot"]["resources"][0]["version_id"], version_id)
        reviews = self.request("GET", f"/api/projects/{project_id}/reviews?target_type=experiment&target_id={exp_id}")
        self.assertEqual(len(reviews["requests"]), 1)
        self.assertEqual(reviews["requests"][0]["target_snapshot"]["resources"][0]["version_id"], version_id)
        queue = self.request("GET", f"/api/projects/{project_id}/reviews")
        self.assertEqual(queue["requests"][0]["target_snapshot"]["resources"][0]["version_id"], version_id)

    def test_review_start_and_submit_are_scoped_to_route_project(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Scoped A"})
        pid = project["id"]
        exp = self.request("POST", f"/api/projects/{pid}/experiments", {"name": "exp-1", "intent": "Scoped review"})
        exp_id = exp["id"]
        (self.repo / "plan.md").write_text(
            "## Summary\nScoped review.\n\n"
            "## Objective & hypothesis\nTest scoping.\n\n"
            "## Evaluation\nMetric: pass/fail of the scoping check.\n"
        )
        plan = self.request("POST", f"/api/projects/{pid}/resources", {"path": "plan.md", "kind": "plan"})
        self.request("POST", f"/api/projects/{pid}/resources/{plan['id']}/associate", {"target_type": "experiment", "target_id": exp_id, "role": "plan"})
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
            json={"review_session_id": session["review_session_id"], "verdict": "pass"},
        )
        self.assertEqual(wrong_submit.status_code, 404, wrong_submit.text)

        # Submitting under the owning project still works.
        self.request("POST", f"/api/projects/{pid}/reviews/submit", {"review_session_id": session["review_session_id"], "verdict": "pass"})

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
        self.assertEqual(requested["ssh"]["command"], f".research_plugin/sbx {sandbox_uid}")
        self.assertTrue(requested["ssh"]["raw_command"].startswith("ssh -i "))

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
        with patch("backend.mlflow.tracking.snapshot_mlflow", return_value=None):
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
        with patch("backend.mlflow.tracking.snapshot_mlflow", return_value=snapshot):
            live = self.request("GET", url)
        self.assertTrue(live["available"])
        self.assertNotIn("base_url", live)

        self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/release")
        with patch("backend.mlflow.tracking.snapshot_mlflow", return_value=snapshot):
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
                    "name": f"rp/{project_id}/{exp_id}",
                    "runs": [
                        {
                            "run_id": "r1",
                            "run_name": "seed_0",
                            "status": "FINISHED",
                            "params": {"lr": "0.001"},
                            "metrics": {"acc": {"last": 0.92}},
                            "history": {"acc": [[1, 0.5], [2, 0.92]]},
                        }
                    ],
                }
            ],
        }

        with patch("backend.mlflow.tracking.snapshot_mlflow", return_value=snapshot):
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
        run = item["metrics"]["experiments"][0]["runs"][0]
        self.assertEqual(run["history"]["acc"], [[1, 0.5], [2, 0.92]])

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
            "experiment_name": f"rp/{project_id}/{exp_id}",
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
        self.assertEqual(transitioned["mlflow"]["experiment_name"], f"rp/{project_id}/{exp_id}")
        self.assertEqual(transitioned["mlflow"]["env"]["MLFLOW_TRACKING_URI"], "https://mlflow.test")
        self.assertEqual(transitioned["mlflow"]["run"]["run_id"], "run_123")
        self.assertEqual(transitioned["mlflow"]["env"]["MLFLOW_RUN_ID"], "run_123")
        self.assertIn("MLflow", transitioned["mlflow_guidance"])

        state = self.app.call_tool(
            "experiment.get_state",
            {"project_id": project_id, "experiment_id": exp_id},
        )
        self.assertTrue(state["mlflow"]["configured"])
        self.assertEqual(state["mlflow"]["experiment_name"], f"rp/{project_id}/{exp_id}")
        self.assertEqual(state["mlflow_run"]["run_id"], "run_123")
        self.assertEqual(state["mlflow"]["run"]["run_id"], "run_123")
        self.assertIn("MLflow", state["mlflow_guidance"])

        http_state = self.request("GET", f"/api/projects/{project_id}/experiments/{exp_id}")
        self.assertTrue(http_state["mlflow"]["configured"])
        self.assertEqual(http_state["mlflow"]["experiment_name"], f"rp/{project_id}/{exp_id}")
        self.assertEqual(http_state["mlflow"]["run"]["run_id"], "run_123")

        # The standalone MLflow context tool returns the same block on demand.
        ctx = self.app.call_tool(
            "mlflow.context",
            {"project_id": project_id, "experiment_id": exp_id},
        )
        self.assertEqual(ctx["scope"], "experiment")
        self.assertTrue(ctx["mlflow"]["configured"])
        self.assertEqual(ctx["mlflow"]["experiment_name"], f"rp/{project_id}/{exp_id}")
        self.assertIn("MLFLOW_EXPERIMENT_NAME", ctx["mlflow"]["env"])
        self.assertEqual(ctx["mlflow"]["run"]["run_id"], "run_123")
        self.assertEqual(ctx["mlflow"]["env"]["MLFLOW_RUN_ID"], "run_123")

        # Without an experiment id it gives project-level navigation context.
        project_ctx = self.app.call_tool("mlflow.context", {"project_id": project_id})
        self.assertEqual(project_ctx["scope"], "project")
        self.assertEqual(project_ctx["mlflow"]["tracking_uri"], "https://mlflow.test")
        self.assertEqual(
            project_ctx["mlflow"]["experiment_namespace_prefix"], f"rp/{project_id}/"
        )
        self.assertEqual(
            project_ctx["mlflow"]["experiments"][0]["mlflow_experiment_name"],
            f"rp/{project_id}/{exp_id}",
        )
        self.assertNotIn("MLFLOW_EXPERIMENT_NAME", project_ctx["mlflow"]["env"])

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
        self.assertIn("RESEARCH_PLUGIN_MLFLOW_TRACKING_URI", ctx["guidance"])

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
        self.assertEqual(activity["activity_log"], str(self.app.activity.log_path))
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
        (self.repo / "plan.md").write_text(
            "## Summary\nCompare LoRA ranks.\n\n"
            "## Objective & hypothesis\nRank 8 suffices.\n\n"
            "## Evaluation\nMetric: eval loss delta; success if within 0.05.\n"
        )
        plan = self.request("POST", f"/api/projects/{pid}/resources", {"path": "plan.md", "kind": "plan", "title": "Plan"})
        self.request(
            "POST",
            f"/api/projects/{pid}/resources/{plan['id']}/associate",
            {"target_type": "experiment", "target_id": exp_id, "role": "plan"},
        )

        figure = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/figure")
        nodes = {node["id"]: node for node in figure["nodes"]}
        edge_ids = {edge["id"] for edge in figure["edges"]}
        self.assertEqual(figure["source"], "derived")
        self.assertEqual(figure["attempt_index"], 1)
        self.assertEqual(nodes["attempt:1"]["status"], "pending")
        self.assertEqual(nodes[f"res:{plan['id']}:a1"]["sublabel"], "plan")
        self.assertIn(f"res:{plan['id']}:a1->attempt:1:feeds", edge_ids)
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
            {"review_session_id": session["review_session_id"], "verdict": "needs_changes"},
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
        (self.repo / "plan.md").write_text(
            "## Summary\nWarmup sweep.\n\n"
            "## Objective & hypothesis\nWarmup changes accuracy.\n\n"
            "## Evaluation\nMetric: accuracy delta; success if > 1pt.\n"
        )
        plan = self.request("POST", f"/api/projects/{pid}/resources", {"path": "plan.md", "kind": "plan"})
        self.request(
            "POST",
            f"/api/projects/{pid}/resources/{plan['id']}/associate",
            {"target_type": "experiment", "target_id": exp_id, "role": "plan"},
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
            {"review_session_id": session["review_session_id"], "verdict": "pass"},
        )

        (self.repo / "results.json").write_text('{"accuracy": 0.93}\n')
        result = self.request("POST", f"/api/projects/{pid}/resources", {"path": "results.json", "kind": "result"})
        (self.repo / "notes.md").write_text("unregistered scratch notes\n")
        graph_body = {
            "version": 1,
            "nodes": [
                {"id": "obj", "kind": "objective", "label": "Warmup sweep",
                 "refs": [claim["id"], exp_id]},
                {"id": "rev", "kind": "pivot", "label": "Design review passed",
                 "refs": [review["id"]]},
                {"id": "out", "kind": "outcome", "label": "Accuracy 93%",
                 "refs": ["results.json", f"{plan['id']}", "notes.md", "ghost.json"]},
            ],
            "edges": [{"from": "obj", "to": "rev"}, {"from": "rev", "to": "out"}],
        }
        import json as _json

        (self.repo / "graph.json").write_text(_json.dumps(graph_body))
        graph_res = self.request("POST", f"/api/projects/{pid}/resources", {"path": "graph.json", "kind": "other"})
        self.request(
            "POST",
            f"/api/projects/{pid}/resources/{graph_res['id']}/associate",
            {"target_type": "experiment", "target_id": exp_id, "role": "graph"},
        )

        payload = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["problems"], [])
        self.assertEqual(len(payload["graph"]["nodes"]), 3)
        refs = payload["ref_index"]
        # Repo-relative path of a registered resource → resource link.
        self.assertEqual(refs["results.json"]["type"], "resource")
        self.assertTrue(refs["results.json"]["resolved"])
        self.assertEqual(refs["results.json"]["resource_id"], result["id"])
        # res_ id → the same resource shape.
        self.assertEqual(refs[plan["id"]]["type"], "resource")
        self.assertEqual(refs[plan["id"]]["path"], "plan.md")
        # rev_ / claim_ / exp_ ids → their records.
        self.assertEqual(refs[review["id"]]["type"], "review")
        self.assertEqual(refs[review["id"]]["verdict"], "pass")
        self.assertEqual(refs[claim["id"]]["type"], "claim")
        self.assertEqual(refs[claim["id"]]["statement"], "Warmup matters.")
        self.assertEqual(refs[exp_id]["type"], "experiment")
        # Unregistered path (whether or not a file exists on disk): unresolved
        # with register-the-file guidance — path refs resolve against the
        # resource records only, never a disk probe.
        for unregistered in ("notes.md", "ghost.json"):
            self.assertFalse(refs[unregistered]["resolved"])
            self.assertIn("not a registered resource", refs[unregistered]["hint"])

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

        # Associate two graph-role files in the same attempt. The alphabetically
        # later path goes FIRST, so a last-by-path picker would choose it; the
        # endpoint must instead pick the most recently associated file — the
        # same row the submit_results validator lints.
        (self.repo / "b_old.json").write_text(graph_with("old story"))
        (self.repo / "a_new.json").write_text(graph_with("new story"))
        for path in ("b_old.json", "a_new.json"):
            res = self.request("POST", f"/api/projects/{pid}/resources", {"path": path, "kind": "other"})
            self.request(
                "POST",
                f"/api/projects/{pid}/resources/{res['id']}/associate",
                {"target_type": "experiment", "target_id": exp_id, "role": "graph"},
            )

        payload = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["path"], "a_new.json")
        self.assertEqual(payload["graph"]["nodes"][0]["label"], "new story")

        # Corrupting the LIVE file is invisible — the endpoint renders the
        # submitted bytes, exactly what the validator lints.
        (self.repo / "a_new.json").write_text("{not json")
        payload = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["graph"]["nodes"][0]["label"], "new story")
        # Re-associating the corrupted file submits it: still available (a
        # graph exists), problems stated — the UI renders them instead of hiding.
        res = self.request("POST", f"/api/projects/{pid}/resources", {"path": "a_new.json", "kind": "other"})
        self.request(
            "POST",
            f"/api/projects/{pid}/resources/{res['id']}/associate",
            {"target_type": "experiment", "target_id": exp_id, "role": "graph"},
        )
        payload = self.request("GET", f"/api/projects/{pid}/experiments/{exp_id}/graph")
        self.assertTrue(payload["available"])
        self.assertIsNone(payload["graph"])
        self.assertTrue(any("not valid JSON" in p for p in payload["problems"]))

    def test_synthesis_endpoints_and_project_graph(self) -> None:
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

        listing = self.request("GET", f"/api/projects/{pid}/syntheses")
        self.assertEqual(len(listing["syntheses"]), 1)
        self.assertEqual(listing["open_synthesis"]["id"], syn_id)
        self.assertIn("signal", listing)

        # No graph yet: the project-graph endpoint degrades, not errors.
        empty = self.request("GET", f"/api/projects/{pid}/syntheses/current/graph")
        self.assertFalse(empty["available"])

        for lens in ("amplify", "avoid", "entropy", "rigor", "cost"):
            path = self.repo / f"syntheses/{syn_id}/reflections/{lens}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{lens} findings\n")
            res = self.request(
                "POST",
                f"/api/projects/{pid}/resources",
                {"path": str(path.relative_to(self.repo))},
            )
            self.request(
                "POST",
                f"/api/projects/{pid}/resources/{res['id']}/associate",
                {"target_type": "reflection", "target_id": syn_id, "role": "reflection_lens_doc"},
            )
        self.app.call_tool(
            "reflection.transition",
            {"project_id": pid, "reflection_id": syn_id, "transition": "submit_reflections"},
        )

        # The project graph refs the wave itself (syn_) and a reflection file.
        (self.repo / "project").mkdir()
        (self.repo / "project/logic_graph.json").write_text(
            '{"version": 1, "title": "Project logic", "nodes": ['
            '{"id": "a", "kind": "lesson", "label": "Lesson", "refs": ["' + syn_id + '"]},'
            '{"id": "b", "kind": "open", "label": "Open question", '
            '"refs": ["syntheses/' + syn_id + '/reflections/avoid.md"]}],'
            ' "edges": [{"from": "a", "to": "b"}]}'
        )
        (self.repo / "project" / "figures").mkdir()
        (self.repo / "project" / "figures" / "project_graph.png").write_bytes(
            b"\x89PNG\r\n\x1a\nfake"
        )
        (self.repo / "project/reflection.md").write_text(
            "# Synthesis\n\n"
            "## Summary\nHTTP synthesis test wave.\n\n"
            "![project graph](figures/project_graph.png)\n\n"
            "## Critical reading\nThe test wave adds one claim and two planned experiments.\n\n"
            "## Decision / future directions\nCreate both HTTP experiments in parallel.\n"
        )
        (self.repo / "project/change_spec.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "claim_changes": [
                        {
                            "op": "create",
                            "key": "claim_http_wave",
                            "statement": "HTTP synthesis wave claim.",
                            "confidence": "medium",
                            "rationale": "The HTTP synthesis test needs a materializable claim.",
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
        reflection_doc_res_id = ""
        for path, role in (
            ("project/logic_graph.json", "project_graph"),
            ("project/reflection.md", "reflection_doc"),
            ("project/change_spec.json", "change_spec"),
        ):
            res = self.request("POST", f"/api/projects/{pid}/resources", {"path": path})
            if role == "reflection_doc":
                reflection_doc_res_id = res["id"]
            self.request(
                "POST",
                f"/api/projects/{pid}/resources/{res['id']}/associate",
                {"target_type": "reflection", "target_id": syn_id, "role": role},
            )
        (self.repo / "project" / "figures" / "project_graph.png").unlink()
        figure = self.client.get(
            f"/api/projects/{pid}/resources/{reflection_doc_res_id}/file",
            params={"rel": "figures/project_graph.png"},
        )
        self.assertEqual(figure.status_code, 200)
        self.assertEqual(figure.headers["content-type"], "image/png")
        self.assertTrue(figure.content.startswith(b"\x89PNG"))
        self.app.call_tool(
            "reflection.transition",
            {"project_id": pid, "reflection_id": syn_id, "transition": "submit_reflection_artifacts"},
        )

        # The open wave's graph renders while still under review.
        payload = self.request("GET", f"/api/projects/{pid}/syntheses/current/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["synthesis"]["id"], syn_id)
        self.assertEqual(payload["synthesis"]["status"], "synthesis_review")
        self.assertEqual(payload["problems"], [])
        refs = payload["ref_index"]
        self.assertEqual(refs[syn_id]["type"], "synthesis")
        self.assertTrue(refs[syn_id]["resolved"])
        self.assertEqual(refs[syn_id]["title"], "Wave 1")
        reflection_ref = refs[f"syntheses/{syn_id}/reflections/avoid.md"]
        self.assertEqual(reflection_ref["type"], "resource")

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
            {"review_session_id": session["review_session_id"], "verdict": "pass"},
        )
        self.app.call_tool(
            "reflection.transition",
            {"project_id": pid, "reflection_id": syn_id, "transition": "publish"},
        )

        detail = self.request("GET", f"/api/projects/{pid}/syntheses/{syn_id}")
        self.assertEqual(detail["status"], "published")
        self.assertTrue(detail["published_graph_version_id"])
        self.assertEqual(len(detail["roster"]), 5)

        # Published wave still serves the living graph as "current".
        payload = self.request("GET", f"/api/projects/{pid}/syntheses/current/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["synthesis"]["status"], "published")
        listing = self.request("GET", f"/api/projects/{pid}/syntheses")
        self.assertIsNone(listing["open_synthesis"])
        self.assertEqual(listing["latest_published"]["id"], syn_id)
        self.assertEqual(listing["current"]["id"], syn_id)

    def test_per_wave_graph_and_versioned_content_endpoints(self) -> None:
        # The reflection-wave UI renders a SPECIFIC wave's graph + content from
        # the bytes it pinned, so it stays faithful after later waves overwrite
        # the living files. Exercise the two endpoints that back that.
        project = self.request("POST", "/api/projects", {"name": "Pin"})
        pid = project["id"]
        syn = self.app.call_tool(
            "reflection.create",
            {
                "project_id": pid,
                "title": "Wave 1",
                "lenses": [
                    {"id": "amplify"},
                    {"id": "avoid"},
                    {"id": "entropy"},
                    {"id": "rigor", "charter": "Method soundness.", "why_distinct": "How, not what."},
                    {"id": "cost", "charter": "Compute spent.", "why_distinct": "Prices exploration."},
                ],
            },
        )
        syn_id = syn["id"]

        # Before any graph is associated, the per-wave endpoint degrades cleanly.
        empty = self.request("GET", f"/api/projects/{pid}/syntheses/{syn_id}/graph")
        self.assertFalse(empty["available"])

        (self.repo / "project").mkdir()
        graph_text = (
            '{"version": 1, "title": "Wave 1 logic", "nodes": ['
            '{"id": "a", "kind": "lesson", "label": "A lesson"}], "edges": []}'
        )
        (self.repo / "project/logic_graph.json").write_text(graph_text)
        graph_res = self.request(
            "POST", f"/api/projects/{pid}/resources", {"path": "project/logic_graph.json"}
        )
        self.request(
            "POST",
            f"/api/projects/{pid}/resources/{graph_res['id']}/associate",
            {"target_type": "reflection", "target_id": syn_id, "role": "project_graph"},
        )

        # The per-wave graph renders the wave's pinned bytes.
        payload = self.request("GET", f"/api/projects/{pid}/syntheses/{syn_id}/graph")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["synthesis"]["id"], syn_id)
        self.assertEqual(payload["graph"]["nodes"][0]["id"], "a")
        self.assertEqual(payload["problems"], [])

        # Find the graph association's pinned version_id off the wave detail.
        detail = self.request("GET", f"/api/projects/{pid}/syntheses/{syn_id}")
        graph_row = next(
            r for r in detail["resources"] if r["association_role"] == "project_graph"
        )
        version_id = graph_row["association_version_id"]
        self.assertTrue(version_id)

        # Versioned content serves the exact submitted bytes.
        pinned = self.request(
            "GET",
            f"/api/projects/{pid}/resources/{graph_res['id']}/content?version={version_id}",
        )
        self.assertEqual(pinned["content"], graph_text)
        self.assertEqual(pinned["source"], "submitted")
        self.assertEqual(pinned["version_id"], version_id)

        # No version → unchanged behavior (still serves the gated bytes).
        plain = self.request(
            "GET", f"/api/projects/{pid}/resources/{graph_res['id']}/content"
        )
        self.assertEqual(plain["content"], graph_text)

        # A version that is not this resource's is rejected, not served.
        bad = self.client.get(
            f"/api/projects/{pid}/resources/{graph_res['id']}/content?version=ver_bogus"
        )
        self.assertEqual(bad.status_code, 404)

        # The literal current/graph route still resolves (not captured by the
        # {synthesis_id}/graph param route).
        current = self.request("GET", f"/api/projects/{pid}/syntheses/current/graph")
        self.assertTrue(current["available"])
        self.assertEqual(current["synthesis"]["id"], syn_id)

    def test_no_version_content_serves_current_version_not_oldest_pin(self) -> None:
        # A living file (project/reflection.md) pinned by two reflection waves is
        # one resource with two versions. The no-version content default is
        # documented as the latest submitted bytes, so it must resolve to the
        # resource's current_version_id (wave 2), NOT whichever association
        # carries the highest per-target attempt index — wave 1 here, simulating
        # a wave that needed extra review rounds, would otherwise win and serve
        # stale bytes. The explicit ?version= path must still serve wave 1's.
        roster = [
            {"id": "amplify"},
            {"id": "avoid"},
            {"id": "entropy"},
            {"id": "rigor", "charter": "Method soundness.", "why_distinct": "How, not what."},
            {"id": "cost", "charter": "Compute spent.", "why_distinct": "Prices exploration."},
        ]
        project = self.request("POST", "/api/projects", {"name": "Pin2"})
        pid = project["id"]
        wave1_id = self.app.call_tool(
            "reflection.create", {"project_id": pid, "title": "Wave 1", "lenses": roster}
        )["id"]
        # Wave 1 went through extra review rounds → a higher attempt index than a
        # fresh wave. Set it before associating so the association row records it.
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE syntheses SET attempt_index = 5 WHERE id = ?", (wave1_id,)
            )

        (self.repo / "project").mkdir()
        refl = self.repo / "project/reflection.md"
        old_text = "# Reflection\n\nWave 1 lessons.\n"
        refl.write_text(old_text)
        res = self.request(
            "POST", f"/api/projects/{pid}/resources", {"path": "project/reflection.md"}
        )
        rid = res["id"]
        self.request(
            "POST",
            f"/api/projects/{pid}/resources/{rid}/associate",
            {"target_type": "reflection", "target_id": wave1_id, "role": "reflection_doc"},
        )
        detail1 = self.request("GET", f"/api/projects/{pid}/syntheses/{wave1_id}")
        old_version = next(
            r for r in detail1["resources"] if r["association_role"] == "reflection_doc"
        )["association_version_id"]
        self.assertTrue(old_version)

        # Close wave 1 so a second wave may open (only one wave edits the living
        # project graph at a time); the content endpoint under test is
        # status-agnostic, so publishing the full gated path is unnecessary here.
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE syntheses SET status = 'published' WHERE id = ?", (wave1_id,)
            )

        wave2_id = self.app.call_tool(
            "reflection.create", {"project_id": pid, "title": "Wave 2", "lenses": roster}
        )["id"]
        new_text = "# Reflection\n\nWave 2 lessons — supersedes wave 1 entirely.\n"
        refl.write_text(new_text)
        self.request(
            "POST",
            f"/api/projects/{pid}/resources/{rid}/associate",
            {"target_type": "reflection", "target_id": wave2_id, "role": "reflection_doc"},
        )
        detail2 = self.request("GET", f"/api/projects/{pid}/syntheses/{wave2_id}")
        new_version = next(
            r for r in detail2["resources"] if r["association_role"] == "reflection_doc"
        )["association_version_id"]
        self.assertTrue(new_version)
        self.assertNotEqual(new_version, old_version)

        # No version → the latest submitted bytes (wave 2 / current_version_id),
        # even though wave 1's association carries the higher attempt index.
        plain = self.request(
            "GET", f"/api/projects/{pid}/resources/{rid}/content"
        )
        self.assertEqual(plain["content"], new_text)
        self.assertEqual(plain["source"], "submitted")
        self.assertEqual(plain["version_id"], new_version)

        # Explicit old version still serves wave 1's pinned bytes faithfully.
        old = self.request(
            "GET", f"/api/projects/{pid}/resources/{rid}/content?version={old_version}"
        )
        self.assertEqual(old["content"], old_text)
        self.assertEqual(old["source"], "submitted")
        self.assertEqual(old["version_id"], old_version)


class ResourceRelFileTest(unittest.TestCase):
    """GET /resources/{id}/file?rel=… serves a file next to the resource (a
    report's figure), locked inside the repo root."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(create_fastapi_app(self.app))
        project = self.client.post("/api/projects", json={"name": "Rel"}).json()
        self.project_id = project["id"]
        (self.repo / "exp").mkdir()
        (self.repo / "exp" / "report.md").write_text("![loss](figures/loss.png)\n")
        (self.repo / "exp" / "figures").mkdir()
        (self.repo / "exp" / "figures" / "loss.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.resource_id = self.client.post(
            f"/api/projects/{self.project_id}/resources",
            json={"path": "exp/report.md", "kind": "report"},
        ).json()["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_serves_sibling_figure(self) -> None:
        response = self.client.get(
            f"/api/projects/{self.project_id}/resources/{self.resource_id}/file",
            params={"rel": "figures/loss.png"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG"))

    def test_serves_submitted_plan_figure_without_live_file(self) -> None:
        exp = self.client.post(
            f"/api/projects/{self.project_id}/experiments",
            json={"name": "plan-figure", "intent": "Verify submitted plan image."},
        ).json()
        (self.repo / "plans" / "figures").mkdir(parents=True)
        figure_path = self.repo / "plans" / "figures" / "diagram.png"
        figure_path.write_bytes(b"\x89PNG\r\n\x1a\nplan")
        (self.repo / "plans" / "plan.md").write_text(
            "## Summary\nPlan with a diagram.\n\n"
            "![diagram](figures/diagram.png)\n\n"
            "## Objective & hypothesis\nTest plan image serving.\n\n"
            "## Evaluation\nSuccess means the backend serves submitted bytes.\n"
        )
        resource = self.client.post(
            f"/api/projects/{self.project_id}/resources",
            json={"path": "plans/plan.md", "kind": "plan"},
        ).json()
        assoc = self.client.post(
            f"/api/projects/{self.project_id}/resources/{resource['id']}/associate",
            json={"target_type": "experiment", "target_id": exp["id"], "role": "plan"},
        )
        self.assertLess(assoc.status_code, 400, assoc.text)

        figure_path.unlink()
        response = self.client.get(
            f"/api/projects/{self.project_id}/resources/{resource['id']}/file",
            params={"rel": "figures/diagram.png"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, b"\x89PNG\r\n\x1a\nplan")

    def test_rejects_escape_outside_repo_root(self) -> None:
        response = self.client.get(
            f"/api/projects/{self.project_id}/resources/{self.resource_id}/file",
            params={"rel": "../../../../etc/hosts"},
        )
        self.assertGreaterEqual(response.status_code, 400)

    def test_missing_sibling_is_not_found(self) -> None:
        response = self.client.get(
            f"/api/projects/{self.project_id}/resources/{self.resource_id}/file",
            params={"rel": "figures/nope.png"},
        )
        self.assertGreaterEqual(response.status_code, 400)


class FigureViewTest(unittest.TestCase):
    def test_resource_fanout_rolls_up_past_cap(self) -> None:
        from backend.services.figure_view import RESOURCE_FANOUT_CAP, build_experiment_figure

        experiment = {
            "id": "exp_x",
            "intent": "Stress the figure.",
            "status": "running",
            "attempt_index": 1,
            "conclusion": "",
            "tested_claims": [],
            "reviews": [],
            "resources": [
                {
                    "id": f"res_{i:03d}",
                    "path": f"results/file_{i:03d}.json",
                    "title": "",
                    "kind": "result",
                    "association_role": "result",
                    "association_attempt_index": 1,
                    "association_version_id": None,
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
        resource_nodes = [n for n in figure["nodes"] if n["type"] == "resource"]
        group_nodes = [n for n in figure["nodes"] if n["type"] == "resource_group"]
        self.assertEqual(len(resource_nodes), RESOURCE_FANOUT_CAP)
        self.assertEqual(len(group_nodes), 1)
        self.assertEqual(group_nodes[0]["meta"]["count"], 20 - RESOURCE_FANOUT_CAP)
        self.assertIn("attempt:1->resgroup:a1:down:produced", {e["id"] for e in figure["edges"]})
        # Live attempt status flows through to the spine node.
        attempt = next(n for n in figure["nodes"] if n["id"] == "attempt:1")
        self.assertEqual(attempt["status"], "active")


class RoutedResearchPluginHttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.router = ProjectRouter(
            registry_db_path=self.root / "registry.sqlite",
            execution_backend_factory=lambda _repo: FakeSandboxBackend(),
        )
        self.client = TestClient(create_fastapi_app(router=self.router))

    def tearDown(self) -> None:
        self.router.shutdown()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        response = self.client.request(method, path, json=body)
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

    def test_project_create_requires_directory_and_routes_by_project(self) -> None:
        missing_dir = self.client.request("POST", "/api/projects", json={"name": "No Dir"})
        self.assertEqual(missing_dir.status_code, 400, missing_dir.text)
        self.assertIn("repo_root", missing_dir.text)

        repo_a = self.root / "project-a"
        repo_b = self.root / "project-b"
        project_a = self.request(
            "POST",
            "/api/projects",
            {"name": "Project A", "summary": "Alpha", "repo_root": str(repo_a)},
        )
        project_b = self.request(
            "POST",
            "/api/projects",
            {"name": "Project B", "summary": "Beta", "repo_root": str(repo_b)},
        )
        self.assertEqual(project_a["repo_root"], str(repo_a.resolve()))
        self.assertEqual(project_b["repo_root"], str(repo_b.resolve()))
        self.assertTrue((repo_a / ".research_plugin" / "state.sqlite").exists())
        self.assertTrue((repo_b / ".research_plugin" / "state.sqlite").exists())

        projects = self.request("GET", "/api/projects")["projects"]
        self.assertEqual({p["id"] for p in projects}, {project_a["id"], project_b["id"]})
        self.assertEqual(
            {p["repo_root"] for p in projects},
            {str(repo_a.resolve()), str(repo_b.resolve())},
        )

        (repo_a / "result.txt").write_text("owned by a\n")
        resource_a = self.request(
            "POST",
            f"/api/projects/{project_a['id']}/resources",
            {"path": "result.txt", "kind": "result"},
        )
        self.assertEqual(resource_a["path"], "result.txt")

        wrong_project = self.client.request(
            "POST",
            f"/api/projects/{project_b['id']}/resources",
            json={"path": "result.txt", "kind": "result"},
        )
        self.assertEqual(wrong_project.status_code, 404, wrong_project.text)

    def test_duplicate_directory_is_rejected(self) -> None:
        repo = self.root / "same-project"
        self.request("POST", "/api/projects", {"name": "One", "repo_root": str(repo)})
        duplicate = self.client.request(
            "POST",
            "/api/projects",
            json={"name": "Two", "repo_root": str(repo)},
        )
        self.assertEqual(duplicate.status_code, 400, duplicate.text)
        self.assertIn("already exists", duplicate.text)


class DegradedStatesTest(unittest.TestCase):
    """Control-mode content reads return documented degraded shapes, not 500s
    (cloud plan Phase 9, open decision F)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(create_fastapi_app(self.app), raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _result_resource(self) -> tuple[str, str]:
        project = self.client.post(
            "/api/projects", json={"name": "Proj P", "summary": "s"}
        ).json()
        pid = project["id"]
        # A result-role file exists on disk locally so registration succeeds...
        (self.repo / "results.json").write_text('{"acc": 0.9}', encoding="utf-8")
        res = self.client.post(
            f"/api/projects/{pid}/resources",
            json={"path": "results.json", "kind": "result"},
        ).json()
        return pid, res["id"]

    def test_result_content_degrades_in_control_mode(self) -> None:
        pid, rid = self._result_resource()
        # ...but a hosted/control HTTP presentation has no local data plane to
        # read it from.
        body = ResearchHttpApi(
            app=self.app, expose_local_data_plane=False
        ).resource_content(project_id=pid, resource_id=rid)
        self.assertFalse(body["available"])
        self.assertEqual(body["reason"], "content_unavailable_in_this_mode")
        self.assertIsNone(body["content"])

    def test_result_content_is_live_in_local_mode(self) -> None:
        pid, rid = self._result_resource()
        resp = self.client.get(f"/api/projects/{pid}/resources/{rid}/content")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertIn("acc", body["content"])

    def test_figure_file_degrades_in_control_mode(self) -> None:
        pid, rid = self._result_resource()
        with self.assertRaises(ContentUnavailableError) as ctx:
            ResearchHttpApi(app=self.app, expose_local_data_plane=False).resource_file(
                project_id=pid, resource_id=rid, rel="fig.png"
            )
        self.assertEqual(ctx.exception.error_code, "content_unavailable")


if __name__ == "__main__":
    unittest.main()
