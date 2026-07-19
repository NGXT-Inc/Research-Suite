from __future__ import annotations

import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from merv.brain.mlflow.config import resolve_mlflow_mode
from merv.brain.mlflow.local_server import LocalMlflowServer
from merv.brain.mlflow.metrics import MlflowSnapshotError
from merv.brain.mlflow.tracking import CentralMlflowService
from merv.brain.kernel.utils import ValidationError


class _JsonResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RunCreateClient:
    def __init__(self) -> None:
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, url: str, params: dict | None = None) -> _JsonResponse:
        self.gets.append((url, params or {}))
        return _JsonResponse({"experiments": []})

    def post(self, url: str, json: dict | None = None) -> _JsonResponse:
        payload = json or {}
        self.posts.append((url, payload))
        if url.endswith("/experiments/create"):
            return _JsonResponse({"experiment_id": "7"})
        if url.endswith("/runs/create"):
            return _JsonResponse(
                {
                    "run": {
                        "info": {
                            "experiment_id": payload["experiment_id"],
                            "run_id": "run_123",
                            "run_name": payload["run_name"],
                            "status": "RUNNING",
                            "artifact_uri": "s3://mlflow/run_123",
                            "start_time": payload["start_time"],
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected POST {url}")


class _FinalizeRunClient:
    def __init__(self, statuses: list[str], update_status_code: int = 200) -> None:
        self.statuses = statuses
        self.update_status_code = update_status_code
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def post(self, url: str, json: dict | None = None) -> _JsonResponse:
        self.posts.append((url, json or {}))
        if url.endswith("/runs/update"):
            return _JsonResponse({}, status_code=self.update_status_code)
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url: str, params: dict | None = None) -> _JsonResponse:
        payload = params or {}
        self.gets.append((url, payload))
        status = self.statuses.pop(0) if self.statuses else "FINISHED"
        info = {
            "experiment_id": "7",
            "run_id": payload["run_id"],
            "run_name": "exp_456-attempt-1",
            "status": status,
            "artifact_uri": "s3://mlflow/run_123",
            "start_time": 1234000,
        }
        if status != "RUNNING":
            info["end_time"] = 2000000
        return _JsonResponse({"run": {"info": info}})


class MlflowTrackingServiceTest(unittest.TestCase):
    def test_context_uses_stable_project_and_experiment_ids(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.example.test/",
            server_uri="http://mlflow:5000/",
            dashboard_url="https://mlflow-ui.example.test/",
        )

        context = service.context(
            project_id="proj_123",
            experiment_id="exp_456",
            sandbox_id="sb_789",
            execution_backend="lambda_labs",
        ).to_dict()

        self.assertTrue(context["configured"])
        self.assertEqual(context["mode"], "external")
        self.assertEqual(context["tracking_uri"], "https://mlflow.example.test")
        self.assertEqual(context["dashboard_url"], "https://mlflow-ui.example.test")
        self.assertEqual(service.server_uri, "http://mlflow:5000")
        self.assertEqual(context["experiment_name"], "merv/proj_123/exp_456")
        self.assertEqual(
            context["env"]["MLFLOW_TRACKING_URI"], "https://mlflow.example.test"
        )
        self.assertEqual(
            context["env"]["MLFLOW_EXPERIMENT_NAME"], "merv/proj_123/exp_456"
        )
        self.assertEqual(context["env"]["RP_SANDBOX_ID"], "sb_789")
        self.assertEqual(context["env"]["RP_EXECUTION_BACKEND"], "lambda_labs")

    def test_project_context_exposes_navigation_namespace_without_experiment(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.example.test/",
            dashboard_url="https://mlflow-ui.example.test/",
        )

        context = service.project_context(project_id="proj_123")

        self.assertTrue(context["configured"])
        self.assertEqual(context["tracking_uri"], "https://mlflow.example.test")
        self.assertEqual(context["dashboard_url"], "https://mlflow-ui.example.test")
        self.assertEqual(context["project_id"], "proj_123")
        self.assertEqual(context["experiment_namespace_prefix"], "merv/proj_123/")
        self.assertEqual(context["env"]["MLFLOW_TRACKING_URI"], "https://mlflow.example.test")
        self.assertEqual(context["env"]["RP_PROJECT_ID"], "proj_123")
        self.assertNotIn("MLFLOW_EXPERIMENT_NAME", context["env"])

    def test_credentials_ride_only_when_requested(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.example.test/",
            agent_key="rr_sk_agent",
        )
        # Default (UI-facing) blocks never carry the credential pair.
        plain = service.context(project_id="p", experiment_id="e").to_dict()
        self.assertNotIn("MLFLOW_TRACKING_PASSWORD", plain["env"])
        self.assertNotIn("MLFLOW_TRACKING_PASSWORD", service.project_context(project_id="p")["env"])
        # Agent-facing blocks opt in and get the Basic-auth env pair.
        agent = service.context(
            project_id="p", experiment_id="e", include_credentials=True
        ).to_dict()
        self.assertEqual(agent["env"]["MLFLOW_TRACKING_USERNAME"], "rp-agent")
        self.assertEqual(agent["env"]["MLFLOW_TRACKING_PASSWORD"], "rr_sk_agent")
        scoped = service.project_context(project_id="p", include_credentials=True)
        self.assertEqual(scoped["env"]["MLFLOW_TRACKING_PASSWORD"], "rr_sk_agent")
        # No key configured -> opting in adds nothing.
        bare = CentralMlflowService(tracking_uri="https://mlflow.example.test")
        agent_bare = bare.context(
            project_id="p", experiment_id="e", include_credentials=True
        ).to_dict()
        self.assertNotIn("MLFLOW_TRACKING_PASSWORD", agent_bare["env"])

    def test_unconfigured_context_still_names_experiment(self) -> None:
        context = CentralMlflowService().context(
            project_id="proj_123", experiment_id="exp_456"
        ).to_dict()

        self.assertFalse(context["configured"])
        self.assertEqual(context["mode"], "unconfigured")
        self.assertEqual(context["experiment_name"], "merv/proj_123/exp_456")
        self.assertNotIn("MLFLOW_TRACKING_URI", context["env"])
        self.assertIn("note", context)

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            resolve_mlflow_mode({"RESEARCH_PLUGIN_MLFLOW_MODE": "sandbox"})

    def test_health_reports_reachability_separately_from_configuration(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.example.test",
            server_uri="http://mlflow:5000",
            dashboard_url="https://mlflow.example.test",
            health_check=lambda: False,
        )

        health = service.health()

        self.assertTrue(health["configured"])
        self.assertTrue(health["tracking_configured"])
        self.assertTrue(health["read_configured"])
        self.assertFalse(health["reachable"])
        self.assertIn("not reachable", health["note"])

    def test_server_uri_only_allows_backend_reads_but_not_agent_logging(self) -> None:
        service = CentralMlflowService(
            mode="external",
            server_uri="http://mlflow:5000",
            health_check=lambda: True,
        )

        context = service.context(
            project_id="proj_123", experiment_id="exp_456"
        ).to_dict()
        health = service.health()
        snapshot = {
            "source": "mlflow",
            "base_url": "http://mlflow:5000",
            "experiments": [{"name": "merv/proj_123/exp_456", "runs": []}],
        }
        with patch(
            "merv.brain.mlflow.tracking.snapshot_mlflow",
            return_value=snapshot,
        ) as snapshot_mlflow:
            metrics = service.results_metrics(
                project_id="proj_123", experiment_id="exp_456"
            )

        self.assertFalse(context["configured"])
        self.assertNotIn("MLFLOW_TRACKING_URI", context["env"])
        self.assertIn("agents cannot log", context["note"])
        self.assertTrue(health["configured"])
        self.assertFalse(health["tracking_configured"])
        self.assertTrue(health["read_configured"])
        self.assertIn("agents cannot log", health["note"])
        snapshot_mlflow.assert_called_once_with(
            "http://mlflow:5000", experiment_name="merv/proj_123/exp_456"
        )
        self.assertTrue(metrics["available"])
        self.assertNotIn("base_url", metrics)

    def test_results_metrics_distinguishes_unreachable_from_no_runs(self) -> None:
        service = CentralMlflowService(server_uri="http://mlflow:5000")
        with patch("merv.brain.mlflow.tracking.snapshot_mlflow", return_value=None):
            empty = service.results_metrics(project_id="proj", experiment_id="exp")
        with patch(
            "merv.brain.mlflow.tracking.snapshot_mlflow",
            side_effect=MlflowSnapshotError("down"),
        ):
            unreachable = service.results_metrics(
                project_id="proj", experiment_id="exp"
            )

        self.assertFalse(empty["available"])
        self.assertIn("No MLflow runs", empty["hint"])
        self.assertFalse(unreachable["available"])
        self.assertEqual(unreachable["hint"], "MLflow unreachable.")

    def test_namespace_experiments_returns_metadata_and_dashboard_links(self) -> None:
        service = CentralMlflowService(
            server_uri="http://mlflow:5000",
            dashboard_url="https://mlflow.test",
        )
        with patch(
            "merv.brain.mlflow.tracking.search_mlflow_experiments",
            return_value=[
                {"name": "merv/proj/exp", "experiment_id": "7"},
                {"name": "merv/proj/stray", "experiment_id": "8"},
            ],
        ) as search:
            experiments = service.namespace_experiments(project_id="proj")

        search.assert_called_once_with(
            "http://mlflow:5000", name_like="merv/proj/%"
        )
        self.assertEqual(experiments[1]["name"], "merv/proj/stray")
        self.assertEqual(
            experiments[1]["dashboard_experiment_url"],
            "https://mlflow.test/#/experiments/8",
        )

    def test_create_run_creates_experiment_and_returns_resume_identity(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.agent.test",
            server_uri="http://mlflow.internal:5000",
            dashboard_url="https://mlflow.ui.test",
        )
        client = _RunCreateClient()

        with (
            patch("merv.brain.mlflow.tracking.httpx.Client", return_value=client),
            patch("merv.brain.mlflow.tracking.time.time", return_value=1234.0),
        ):
            run = service.create_run(
                project_id="proj_123",
                experiment_id="exp_456",
                attempt_index=2,
            )

        self.assertTrue(run["created"])
        self.assertEqual(run["experiment_name"], "merv/proj_123/exp_456")
        self.assertEqual(run["experiment_id"], "7")
        self.assertEqual(run["run_id"], "run_123")
        self.assertEqual(run["run_name"], "exp_456-attempt-2")
        self.assertEqual(run["status"], "RUNNING")
        self.assertEqual(run["artifact_uri"], "s3://mlflow/run_123")
        self.assertEqual(
            run["dashboard_run_url"],
            "https://mlflow.ui.test/#/experiments/7/runs/run_123",
        )
        self.assertEqual(
            client.gets[0][0],
            "http://mlflow.internal:5000/api/2.0/mlflow/experiments/search",
        )
        run_payload = client.posts[-1][1]
        self.assertEqual(run_payload["experiment_id"], "7")
        self.assertEqual(
            {tag["key"]: tag["value"] for tag in run_payload["tags"]},
            {
                "project_id": "proj_123",
                "experiment_id": "exp_456",
                "attempt_index": "2",
                "created_by": "research_plugin",
            },
        )

    def test_create_run_requires_backend_write_uri(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.agent.test",
        )

        run = service.create_run(project_id="proj_123", experiment_id="exp_456")

        self.assertFalse(run["created"])
        self.assertTrue(run["configured"])
        self.assertFalse(run["control_configured"])
        self.assertIn("MERV_MLFLOW_SERVER_URI", run["note"])

    def test_finalize_run_updates_status_and_reads_back_terminal_state(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.agent.test",
            server_uri="http://mlflow.internal:5000",
            dashboard_url="https://mlflow.ui.test",
        )
        # Pre-update read sees RUNNING, first poll still RUNNING, then FINISHED.
        client = _FinalizeRunClient(["RUNNING", "RUNNING", "FINISHED"])

        with (
            patch("merv.brain.mlflow.tracking.httpx.Client", return_value=client),
            patch("merv.brain.mlflow.tracking.time.time", return_value=2000.0),
            patch("merv.brain.mlflow.tracking.time.sleep") as sleep,
        ):
            result = service.finalize_run(
                project_id="proj_123",
                experiment_id="exp_456",
                run_id="run_123",
                status="FINISHED",
                wait_seconds=1.0,
            )

        self.assertEqual(
            client.posts[0][0],
            "http://mlflow.internal:5000/api/2.0/mlflow/runs/update",
        )
        self.assertEqual(
            client.posts[0][1],
            {"run_id": "run_123", "status": "FINISHED", "end_time": 2000000},
        )
        self.assertEqual(len(client.gets), 3)
        self.assertTrue(result["update"]["applied"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["readback_attempts"], 2)
        self.assertEqual(result["run"]["run_id"], "run_123")
        self.assertEqual(result["run"]["status"], "FINISHED")
        self.assertEqual(result["run"]["ended_at"], "1970-01-01T00:33:20Z")
        self.assertEqual(
            result["run"]["dashboard_run_url"],
            "https://mlflow.ui.test/#/experiments/7/runs/run_123",
        )
        sleep.assert_called()

    def test_finalize_run_reads_back_even_when_status_update_fails(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.agent.test",
            server_uri="http://mlflow.internal:5000",
        )
        client = _FinalizeRunClient(["RUNNING", "FINISHED"], update_status_code=500)

        with (
            patch("merv.brain.mlflow.tracking.httpx.Client", return_value=client),
            patch("merv.brain.mlflow.tracking.time.time", return_value=2000.0),
        ):
            result = service.finalize_run(
                project_id="proj_123",
                experiment_id="exp_456",
                run_id="run_123",
            )

        self.assertFalse(result["update"]["applied"])
        self.assertIn("status update failed", result["update"]["error"])
        self.assertEqual(result["readback_attempts"], 1)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["run"]["status"], "FINISHED")

    def test_finalize_run_readback_only_polls_until_terminal(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.agent.test",
            server_uri="http://mlflow.internal:5000",
        )
        client = _FinalizeRunClient(["RUNNING", "RUNNING", "FINISHED"])

        with (
            patch("merv.brain.mlflow.tracking.httpx.Client", return_value=client),
            patch("merv.brain.mlflow.tracking.time.sleep"),
        ):
            result = service.finalize_run(
                project_id="proj_123",
                experiment_id="exp_456",
                run_id="run_123",
                status=None,
                wait_seconds=5.0,
            )

        # status=null is the documented "script already ended the run" mode;
        # it must absorb the stale immediate RUNNING readback, not persist it.
        self.assertEqual(client.posts, [])
        self.assertEqual(result["readback_attempts"], 3)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["run"]["status"], "FINISHED")

    def test_finalize_run_refuses_to_overwrite_a_terminal_status(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.agent.test",
            server_uri="http://mlflow.internal:5000",
        )
        client = _FinalizeRunClient(["FAILED", "FAILED"])

        with patch("merv.brain.mlflow.tracking.httpx.Client", return_value=client):
            result = service.finalize_run(
                project_id="proj_123",
                experiment_id="exp_456",
                run_id="run_123",
                status="FINISHED",
            )

        # The script already recorded FAILED; the FINISHED default must not
        # rewrite it.
        self.assertEqual(client.posts, [])
        self.assertFalse(result["update"]["applied"])
        self.assertEqual(result["update"]["skipped_already_terminal"], "FAILED")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["run"]["status"], "FAILED")

    def test_finalize_run_errors_never_leak_the_server_uri(self) -> None:
        service = CentralMlflowService(
            mode="external",
            tracking_uri="https://mlflow.agent.test",
            server_uri="http://mlflow.internal:5000",
        )

        class _ExplodingClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def get(self, url: str, params: dict | None = None):
                raise RuntimeError(f"connect failed for {url}")

        with patch(
            "merv.brain.mlflow.tracking.httpx.Client", return_value=_ExplodingClient()
        ):
            result = service.finalize_run(
                project_id="proj_123",
                experiment_id="exp_456",
                run_id="run_123",
                status=None,
            )

        self.assertIn("finalize/readback failed", result["error"])
        self.assertNotIn("mlflow.internal", result["error"])
        self.assertIn("<mlflow-server>", result["error"])


class LocalMlflowServerTest(unittest.TestCase):
    def test_explicit_tracking_uri_disables_local_process(self) -> None:
        env = {
            "RESEARCH_PLUGIN_MLFLOW_MODE": "external",
            "RESEARCH_PLUGIN_MLFLOW_TRACKING_URI": "http://mlflow.example.test",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            with patch("merv.brain.mlflow.local_server.subprocess.Popen") as popen:
                service = LocalMlflowServer(root=Path(tmp)).start()

        popen.assert_not_called()
        self.assertEqual(service.mode, "external")
        self.assertEqual(service.tracking_uri, "http://mlflow.example.test")

    def test_managed_start_returns_local_service(self) -> None:
        process = MagicMock()
        process.pid = 12345
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with (
                patch.object(LocalMlflowServer, "_choose_port", return_value=5678),
                patch.object(LocalMlflowServer, "_wait_until_ready", return_value=True),
                patch("merv.brain.mlflow.local_server.subprocess.Popen", return_value=process) as popen,
                patch("merv.brain.mlflow.local_server.os.killpg") as killpg,
            ):
                server = LocalMlflowServer(root=Path(tmp))
                service = server.start()
                server.stop()

        command = popen.call_args.args[0]
        self.assertEqual(command[:3], [os.sys.executable, "-m", "mlflow"])
        self.assertIn("server", command)
        self.assertIn("--serve-artifacts", command)
        self.assertEqual(service.mode, "managed")
        self.assertEqual(service.tracking_uri, "http://127.0.0.1:5678")
        killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_managed_start_failure_reports_log_path(self) -> None:
        process = MagicMock()
        process.poll.return_value = 1
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with (
                patch.object(LocalMlflowServer, "_choose_port", return_value=5678),
                patch("merv.brain.mlflow.local_server.subprocess.Popen", return_value=process),
            ):
                service = LocalMlflowServer(root=Path(tmp)).start()

        health = service.health()
        self.assertFalse(health["configured"])
        self.assertEqual(health["mode"], "managed")
        self.assertIn("Managed MLflow failed to start", health["note"])

if __name__ == "__main__":
    unittest.main()
