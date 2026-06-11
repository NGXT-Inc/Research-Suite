from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import ResearchPluginApp
from backend.http_api import create_fastapi_app
from backend.project_router import ProjectRouter
from backend.execution.backends.fake import FakeSandboxBackend
from mcp_server.time_utils import now_iso
from tests.fakes import FakeRsyncSyncer


class ResearchPluginHttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
            rsync_syncer=FakeRsyncSyncer(sync_pulled=1, sync_stdout="metrics.json\n"),
        )
        self.client = TestClient(create_fastapi_app(self.app))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        response = self.client.request(method, path, json=body)
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

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
            {"intent": "Compare threshold with baseline.", "claim_ids": [claim["id"]]},
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
        exp = self.request("POST", f"/api/projects/{pid}/experiments", {"intent": "Scoped review"})
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
        exp = self.request("POST", f"/api/projects/{project_id}/experiments", {"intent": "Run an experiment"})
        exp_id = exp["id"]
        # Drive the experiment to ready_to_run so a sandbox may be requested.
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,))
        # Procuring is an agent action (MCP tool); the UI observes the result.
        requested = self.app.call_tool(
            "sandbox.request", {"project_id": project_id, "experiment_id": exp_id, "gpu": "A100"}
        )
        self.assertEqual(requested["status"], "running")
        self.assertEqual(requested["ssh"]["command"], f".research_plugin/sbx {exp_id}")
        self.assertTrue(requested["ssh"]["raw_command"].startswith("ssh -i "))

        sandbox = self.request("GET", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox")
        self.assertEqual(sandbox["status"], "running")
        self.assertTrue(sandbox["sandbox_id"])
        # The HTTP row carries observability dashboard URLs (MLflow + TensorBoard)
        # so the UI can render an iframe tab per non-empty entry.
        self.assertIn("dashboards", sandbox)
        self.assertIn("mlflow", sandbox["dashboards"])
        self.assertTrue(sandbox["dashboards"]["mlflow"].startswith("https://"))

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

        synced = self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/sync")
        self.assertEqual(synced["sync"]["provider"], "ssh_rsync")
        self.assertEqual(synced["sync"]["pulled"], 1)

        released = self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/release")
        self.assertEqual(released["status"], "terminated")

        self.assertTrue(self.request("GET", "/api/sandboxes/health")["ok"])

    def test_results_metrics_endpoint_survives_release(self) -> None:
        # The archived-metrics endpoint is what makes results outlive the VM:
        # empty before any capture, populated after sync, still readable (same
        # payload) after the sandbox is terminated.
        project = self.request("POST", "/api/projects", {"name": "Results Project"})
        project_id = project["id"]
        exp = self.request(
            "POST", f"/api/projects/{project_id}/experiments", {"intent": "Train"}
        )
        exp_id = exp["id"]
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,))
        self.app.call_tool(
            "sandbox.request", {"project_id": project_id, "experiment_id": exp_id, "gpu": "A100"}
        )

        url = f"/api/projects/{project_id}/experiments/{exp_id}/results/metrics"
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
        with patch("backend.services.sandboxes.snapshot_mlflow", return_value=snapshot):
            self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/sync")
        live = self.request("GET", url)
        self.assertTrue(live["available"])
        self.assertEqual(live["sandbox_status"], "running")

        # Release with MLflow already unreachable — the last good archive serves on.
        with patch("backend.services.sandboxes.snapshot_mlflow", return_value=None):
            self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/release")
        durable = self.request("GET", url)
        self.assertTrue(durable["available"])
        self.assertEqual(durable["sandbox_status"], "terminated")
        run = durable["experiments"][0]["runs"][0]
        self.assertEqual(run["metrics"]["acc"]["last"], 0.91)
        self.assertEqual(run["history"]["acc"], [[10, 0.85], [20, 0.91]])

    def test_home_exposes_active_experiments_and_processes(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Active Work Project"})
        project_id = project["id"]
        planned = self.request("POST", f"/api/projects/{project_id}/experiments", {"intent": "Planned active work"})
        running = self.request("POST", f"/api/projects/{project_id}/experiments", {"intent": "Running active work"})
        complete = self.request("POST", f"/api/projects/{project_id}/experiments", {"intent": "Finished work"})
        now = now_iso()
        with self.app.store.transaction() as conn:
            conn.execute("UPDATE experiments SET status = 'running', updated_at = ? WHERE id = ?", (now, running["id"]))
            conn.execute("UPDATE experiments SET status = 'complete', updated_at = ? WHERE id = ?", (now, complete["id"]))
            conn.execute(
                """
                INSERT INTO sandboxes (
                  experiment_id, project_id, sandbox_id, status, created_at, updated_at
                )
                VALUES (?, ?, 'sb_active', 'running', ?, ?)
                """,
                (running["id"], project_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO sandboxes (
                  experiment_id, project_id, sandbox_id, status, terminated_at, created_at, updated_at
                )
                VALUES (?, ?, 'sb_done', 'terminated', ?, ?, ?)
                """,
                (complete["id"], project_id, now, now, now),
            )

        home = self.request("GET", f"/api/projects/{project_id}/home")

        self.assertEqual([item["id"] for item in home["active_experiments"]], [running["id"], planned["id"]])
        self.assertEqual(home["active_experiment"]["id"], running["id"])
        self.assertEqual(home["workflow"]["next_action"], "run_experiment_and_sync_results")
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
            {"intent": "Compare LoRA ranks.", "claim_ids": [claim["id"]]},
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
            rsync_syncer=FakeRsyncSyncer(sync_pulled=1, sync_stdout="metrics.json\n"),
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


if __name__ == "__main__":
    unittest.main()
