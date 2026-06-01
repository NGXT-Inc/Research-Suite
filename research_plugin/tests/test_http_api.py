from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient

from backend.app import ResearchPluginApp
from backend.http_api import create_fastapi_app
from backend.http_server import make_http_server
from backend.execution.backends.fake import FakeBackend
from mcp_server.time_utils import now_iso


class ResearchPluginHttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeBackend()
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
        (self.repo / "plan.md").write_text("metric: accuracy\nbaseline: majority\n")
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
        (self.repo / "plan.md").write_text("plan\n")
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

    def test_job_http_endpoints(self) -> None:
        project = self.request("POST", "/api/projects", {"name": "Job UI Project"})
        project_id = project["id"]
        exp = self.request("POST", f"/api/projects/{project_id}/experiments", {"intent": "Run a job"})
        exp_id = exp["id"]
        (self.repo / "plan.md").write_text("metric: output exists\n")
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "train.py").write_text("print('ok')\n")
        plan = self.request("POST", f"/api/projects/{project_id}/resources", {"path": "plan.md", "kind": "note"})
        self.request("POST", f"/api/projects/{project_id}/resources/{plan['id']}/associate", {"target_type": "experiment", "target_id": exp_id, "role": "plan"})
        self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/transition", {"transition": "submit_design"})
        req = self.request("POST", f"/api/projects/{project_id}/reviews/request", {"target_type": "experiment", "target_id": exp_id, "role": "design_reviewer"})
        session = self.request("POST", f"/api/projects/{project_id}/reviews/start", {"review_request_id": req["review_request_id"], "reviewer_capability": req["reviewer_capability"], "caller_session_id": "design"})
        self.request("POST", f"/api/projects/{project_id}/reviews/submit", {"review_session_id": session["review_session_id"], "verdict": "pass"})
        self.request("POST", f"/api/projects/{project_id}/experiments/{exp_id}/transition", {"transition": "mark_ready_to_run"})
        job = self.request("POST", f"/api/projects/{project_id}/jobs", {"experiment_id": exp_id, "command": "python scripts/train.py", "expected_outputs": ["out.json"]})
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["backend"], "fake")
        self.assertIn("runtime_job_id", job)
        self.assertEqual(self.request("GET", f"/api/projects/{project_id}/jobs/{job['id']}")["status"], "queued")
        runtime_id = job["runtime_job_id"]
        self.backend.logs_by_id[runtime_id] = "http fake logs\n"
        self.assertIn("http fake logs", self.request("GET", f"/api/projects/{project_id}/jobs/{job['id']}/logs")["logs"])
        # Health is process-global (no project scope) since the execution
        # backend is a single instance shared across projects.
        self.assertTrue(self.request("GET", "/api/jobs/health")["ok"])

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
                INSERT INTO jobs (
                  id, project_id, experiment_id, attempt_index, runtime_job_id, backend, command, cwd,
                  expected_outputs_json, backend_hints_json, metadata_json, status,
                  submitted_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 1, ?, 'fake', 'python train.py', '.', '[]', '{}', '{}', 'queued', ?, ?, ?)
                """,
                ("job_active_test", project_id, running["id"], "runtime_active_test", now, now, now),
            )
            conn.execute(
                """
                INSERT INTO jobs (
                  id, project_id, experiment_id, attempt_index, runtime_job_id, backend, command, cwd,
                  expected_outputs_json, backend_hints_json, metadata_json, status,
                  submitted_at, finished_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 1, ?, 'fake', 'python train.py', '.', '[]', '{}', '{}', 'succeeded', ?, ?, ?, ?)
                """,
                ("job_done_test", project_id, complete["id"], "runtime_done_test", now, now, now, now),
            )

        home = self.request("GET", f"/api/projects/{project_id}/home")

        self.assertEqual([item["id"] for item in home["active_experiments"]], [running["id"], planned["id"]])
        self.assertEqual(home["active_experiment"]["id"], running["id"])
        self.assertEqual(home["workflow"]["next_action"], "wait_for_job")
        self.assertEqual(home["stats"]["active_experiments"], 2)
        self.assertEqual(home["stats"]["active_processes"], 1)
        self.assertEqual(home["active_processes"][0]["id"], "job_active_test")
        self.assertEqual(home["active_processes"][0]["process_type"], "execution_job")
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

    def test_live_http_server_smoke(self) -> None:
        server = make_http_server(self.app, "127.0.0.1", 0)
        host, port = server.server_address
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://{host}:{port}"
            health = self.fetch_json(base + "/health")
            self.assertTrue(health["ok"])
            project = self.fetch_json(
                base + "/api/projects",
                method="POST",
                body={"name": "Live UI Project"},
            )
            home = self.fetch_json(base + f"/api/projects/{project['id']}/home")
            self.assertEqual(home["project"]["name"], "Live UI Project")
            activity = self.fetch_json(base + "/api/activity?limit=20")
            self.assertTrue(any(event.get("event") == "http.request" for event in activity["events"]))
        finally:
            server.shutdown()
            server.server_close()

    def fetch_json(self, url: str, *, method: str = "GET", body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=5) as res:
                return json.loads(res.read().decode("utf-8"))
        except HTTPError as exc:
            self.fail(exc.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
