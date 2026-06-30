from __future__ import annotations

import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.config import resolve_mlflow_mode
from backend.daemon.project_router import ProjectRouter
from backend.execution.backends.fake import FakeSandboxBackend
from backend.mlflow.local_server import LocalMlflowServer
from backend.mlflow.tracking import CentralMlflowService
from backend.utils import ValidationError


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
        self.assertEqual(context["experiment_name"], "rp/proj_123/exp_456")
        self.assertEqual(
            context["env"]["MLFLOW_TRACKING_URI"], "https://mlflow.example.test"
        )
        self.assertEqual(
            context["env"]["MLFLOW_EXPERIMENT_NAME"], "rp/proj_123/exp_456"
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
        self.assertEqual(context["experiment_namespace_prefix"], "rp/proj_123/")
        self.assertEqual(context["env"]["MLFLOW_TRACKING_URI"], "https://mlflow.example.test")
        self.assertEqual(context["env"]["RP_PROJECT_ID"], "proj_123")
        self.assertNotIn("MLFLOW_EXPERIMENT_NAME", context["env"])

    def test_unconfigured_context_still_names_experiment(self) -> None:
        context = CentralMlflowService().context(
            project_id="proj_123", experiment_id="exp_456"
        ).to_dict()

        self.assertFalse(context["configured"])
        self.assertEqual(context["mode"], "unconfigured")
        self.assertEqual(context["experiment_name"], "rp/proj_123/exp_456")
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
            "experiments": [{"name": "rp/proj_123/exp_456", "runs": []}],
        }
        with patch(
            "backend.mlflow.tracking.snapshot_mlflow",
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
            "http://mlflow:5000", experiment_name="rp/proj_123/exp_456"
        )
        self.assertTrue(metrics["available"])
        self.assertNotIn("base_url", metrics)


class LocalMlflowServerTest(unittest.TestCase):
    def test_explicit_tracking_uri_disables_local_process(self) -> None:
        env = {
            "RESEARCH_PLUGIN_MLFLOW_MODE": "external",
            "RESEARCH_PLUGIN_MLFLOW_TRACKING_URI": "http://mlflow.example.test",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            with patch("backend.mlflow.local_server.subprocess.Popen") as popen:
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
                patch("backend.mlflow.local_server.subprocess.Popen", return_value=process) as popen,
                patch("backend.mlflow.local_server.os.killpg") as killpg,
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
                patch("backend.mlflow.local_server.subprocess.Popen", return_value=process),
            ):
                service = LocalMlflowServer(root=Path(tmp)).start()

        health = service.health()
        self.assertFalse(health["configured"])
        self.assertEqual(health["mode"], "managed")
        self.assertIn("Managed MLflow failed to start", health["note"])

    def test_local_http_router_shares_one_managed_mlflow_server(self) -> None:
        service = CentralMlflowService(
            mode="managed",
            tracking_uri="http://127.0.0.1:5678",
            server_uri="http://127.0.0.1:5678",
            dashboard_url="http://127.0.0.1:5678",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with (
                patch(
                    "backend.daemon.project_router.LocalMlflowServer.start",
                    return_value=service,
                ) as start,
                patch("backend.daemon.project_router.LocalMlflowServer.stop") as stop,
            ):
                root = Path(tmp)
                router = ProjectRouter(
                    registry_db_path=root / "registry.sqlite",
                    execution_backend_factory=lambda _repo: FakeSandboxBackend(),
                    manage_local_mlflow=True,
                )
                first = router.create_project(repo_root=root / "repo-a", name="Alpha")
                second = router.create_project(repo_root=root / "repo-b", name="Beta")
                self.assertNotEqual(first["id"], second["id"])
                self.assertEqual(
                    router.app_for_project(first["id"]).mlflow_tracking.tracking_uri,
                    service.tracking_uri,
                )
                self.assertEqual(
                    router.app_for_project(second["id"]).mlflow_tracking.tracking_uri,
                    service.tracking_uri,
                )
                router.shutdown()

        start.assert_called_once()
        stop.assert_called_once()

    def test_router_stops_managed_mlflow_when_constructor_fails(self) -> None:
        service = CentralMlflowService(
            mode="managed",
            tracking_uri="http://127.0.0.1:5678",
            server_uri="http://127.0.0.1:5678",
            dashboard_url="http://127.0.0.1:5678",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with (
                patch(
                    "backend.daemon.project_router.LocalMlflowServer.start",
                    return_value=service,
                ) as start,
                patch("backend.daemon.project_router.LocalMlflowServer.stop") as stop,
                patch.object(
                    ProjectRouter,
                    "_resume_active_sandbox_projects",
                    side_effect=RuntimeError("resume failed"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    ProjectRouter(
                        registry_db_path=Path(tmp) / "registry.sqlite",
                        manage_local_mlflow=True,
                    )

        start.assert_called_once()
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
