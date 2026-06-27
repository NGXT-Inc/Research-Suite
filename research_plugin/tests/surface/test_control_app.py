from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.composition.control_mode import build_control_app
from backend.composition import control_mode
from backend.config import (
    ALLOWED_ORIGINS_ENV_VAR,
    BLOB_BUCKET_ENV_VAR,
    CONTROL_RESTRICT_CORS_ENV_VAR,
    DB_URL_ENV_VAR,
    MLFLOW_MODE_ENV_VAR,
    MLFLOW_SERVER_URI_ENV_VAR,
    MLFLOW_TRACKING_URI_ENV_VAR,
    MGMT_KEY_PATH_ENV_VAR,
    MGMT_PUBLIC_KEY_ENV_VAR,
)
from backend.execution.backends.fake import FakeSandboxBackend
from backend.transport.http_api import create_fastapi_app
from backend.transport.http_policy import HttpSurfacePolicy
from backend.state import StateStore
from backend.state.blobs import LocalDirBlobStore
from backend.state.managed_mgmt_keys import MountedMgmtKeyStore
from backend.utils import ValidationError
from backend.version import CLIENT_VERSION_HEADER


def _mounted_mgmt_key_env(root: Path) -> dict[str, str]:
    key_path = root / "managed_key"
    key_path.write_text("PRIVATE KEY\n", encoding="utf-8")
    key_path.chmod(0o600)
    return {
        MGMT_KEY_PATH_ENV_VAR: str(key_path),
        MGMT_PUBLIC_KEY_ENV_VAR: "ssh-ed25519 AAAAmanaged",
    }


class ControlAppTest(unittest.TestCase):
    def test_control_app_records_scoped_activity_without_local_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, _queue = build_control_app(
                repo_root=root,
                env=_mounted_mgmt_key_env(root),
                execution_backend=FakeSandboxBackend(),
            )
            self.addCleanup(app.shutdown)
            client = TestClient(
                create_fastapi_app(
                    app=app,
                    surface_policy=HttpSurfacePolicy.for_surface(
                        restrict_cors=True,
                        hosted_control=True,
                        expose_local_data_plane=False,
                    ),
                ),
                raise_server_exceptions=False,
            )

            created = client.post(
                "/api/projects", json={"name": "Control Telemetry"}
            )
            self.assertEqual(created.status_code, 201, created.text)
            project_id = created.json()["id"]
            claim = client.post(
                f"/api/projects/{project_id}/claims",
                json={"statement": "A scoped control-plane claim."},
            )
            self.assertEqual(claim.status_code, 201, claim.text)

            stats = app.tool_calls.stats(project_id=project_id)
            self.assertGreaterEqual(stats["totals"]["calls"], 1)
            self.assertIn("filter", stats)
            app.tool_calls.record(
                tool="review.start",
                source="http",
                status="ok",
                duration_ms=1,
                arguments={
                    "project_id": project_id,
                    "reviewer_capability": "rp_arg",
                },
                result={"capability": "rp_result"},
            )
            listed = client.get(
                "/api/debug/tool-calls?source=all&status=all",
            )
            self.assertEqual(listed.status_code, 200, listed.text)
            calls = listed.json()["calls"]
            self.assertGreaterEqual(len(calls), 1)
            self.assertTrue(listed.json()["by_tool"])
            review_call = next(call for call in calls if call["tool"] == "review.start")
            detail = client.get(
                f"/api/debug/tool-calls/{review_call['id']}",
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["args"]["reviewer_capability"], "[redacted]")
            self.assertEqual(detail.json()["result"]["capability"], "[redacted]")
            activity = client.get("/api/activity")
            self.assertEqual(activity.status_code, 200, activity.text)
            self.assertGreaterEqual(activity.json()["summary"]["total"], 1)
            names = {tool["name"] for tool in app.list_tools()}
            self.assertIn("claim.create", names)
            self.assertNotIn("resource.register_file", names)

    def test_control_app_uses_mounted_management_key_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _mounted_mgmt_key_env(root)
            key_path = Path(env[MGMT_KEY_PATH_ENV_VAR])
            app, _queue = build_control_app(
                repo_root=root / "staging",
                env=env,
                execution_backend=FakeSandboxBackend(),
            )
            self.addCleanup(app.shutdown)

            self.assertIsInstance(app.sandboxes.mgmt_keys, MountedMgmtKeyStore)
            self.assertEqual(
                app.sandboxes.mgmt_keys.ensure(sandbox_uid="sb_1"),
                "ssh-ed25519 AAAAmanaged",
            )
            self.assertEqual(
                app.sandboxes.mgmt_keys.key_path(sandbox_uid="sb_1"), key_path
            )

    def test_control_app_rejects_partial_management_key_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError):
                build_control_app(
                    repo_root=Path(tmp),
                    env={MGMT_PUBLIC_KEY_ENV_VAR: "ssh-ed25519 AAAAmanaged"},
                    execution_backend=FakeSandboxBackend(),
                )

    def test_control_app_requires_mounted_management_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError) as ctx:
                build_control_app(
                    repo_root=Path(tmp),
                    execution_backend=FakeSandboxBackend(),
                )
        self.assertIn(MGMT_KEY_PATH_ENV_VAR, ctx.exception.message)

    def test_control_app_reads_task_result_timeout_from_injected_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, _queue = build_control_app(
                repo_root=root,
                env={
                    **_mounted_mgmt_key_env(root),
                    "RESEARCH_PLUGIN_TASK_RESULT_TIMEOUT": "2.5",
                },
                execution_backend=FakeSandboxBackend(),
            )
            self.addCleanup(app.shutdown)
            self.assertEqual(app.sandboxes.tasks.result_timeout_seconds, 2.5)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                build_control_app(
                    repo_root=root,
                    env={
                        **_mounted_mgmt_key_env(root),
                        "RESEARCH_PLUGIN_TASK_RESULT_TIMEOUT": "bad",
                    },
                    execution_backend=FakeSandboxBackend(),
                )

    def test_control_app_reads_mlflow_from_injected_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, _queue = build_control_app(
                repo_root=root,
                env={
                    **_mounted_mgmt_key_env(root),
                    MLFLOW_MODE_ENV_VAR: "external",
                    MLFLOW_TRACKING_URI_ENV_VAR: "https://mlflow.example.test/",
                    MLFLOW_SERVER_URI_ENV_VAR: "http://mlflow:5000/",
                },
                execution_backend=FakeSandboxBackend(),
            )
            self.addCleanup(app.shutdown)

            self.assertEqual(app.mlflow_tracking.tracking_uri, "https://mlflow.example.test")
            self.assertEqual(app.mlflow_tracking.server_uri, "http://mlflow:5000")
            self.assertNotIn("mlflow", app.sandboxes.backend_health())

    def test_control_app_lazy_central_metrics_record_without_archive(self) -> None:
        snapshot = {
            "source": "mlflow",
            "base_url": "http://mlflow:5000",
            "experiments": [
                {
                    "name": "central",
                    "runs": [
                        {
                            "run_id": "run_1",
                            "metrics": {"loss": {"last": 0.2}},
                            "params": {},
                            "history": {},
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, _queue = build_control_app(
                repo_root=root,
                env={
                    **_mounted_mgmt_key_env(root),
                    MLFLOW_TRACKING_URI_ENV_VAR: "https://mlflow.example.test/",
                    MLFLOW_SERVER_URI_ENV_VAR: "http://mlflow:5000/",
                },
                execution_backend=FakeSandboxBackend(),
            )
            self.addCleanup(app.shutdown)
            project_id = app.call_tool("project.create", {"name": "Control Metrics"})["id"]
            exp_id = app.call_tool(
                "experiment.create",
                {"project_id": project_id, "name": "exp", "intent": "measure"},
            )["id"]
            app.sandboxes.registry.upsert(
                experiment_id=exp_id,
                sandbox_uid="uid_control_metrics",
                project_id=project_id,
                status="running",
                sandbox_id="sbx_control",
            )

            with patch(
                "backend.services.mlflow_tracking.snapshot_mlflow",
                return_value=dict(snapshot),
            ) as capture:
                result = app.mlflow_tracking.results_metrics(
                    experiment_id=exp_id, project_id=project_id
                )

            capture.assert_called_once()
            self.assertEqual(capture.call_args.args[0], "http://mlflow:5000")
            self.assertTrue(result["available"])
            self.assertNotIn("base_url", result)
            self.assertEqual(result["experiments"][0]["name"], "central")

    def test_control_app_without_repo_root_requires_durable_config(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            build_control_app(repo_root=None, env={}, execution_backend=FakeSandboxBackend())

        self.assertIn(DB_URL_ENV_VAR, ctx.exception.message)
        self.assertIn(BLOB_BUCKET_ENV_VAR, ctx.exception.message)
        self.assertIn(MGMT_KEY_PATH_ENV_VAR, ctx.exception.message)

    def test_control_app_without_repo_root_uses_non_created_compat_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mounted_env = _mounted_mgmt_key_env(root)
            store = StateStore(db_path=root / "state.sqlite")
            blobs = LocalDirBlobStore(root=root / "blobs")
            env = {
                **mounted_env,
                DB_URL_ENV_VAR: "postgresql://user:pass@db/research_plugin",
                BLOB_BUCKET_ENV_VAR: "research-plugin-blobs",
            }
            with (
                patch(
                    "backend.composition.control_mode.build_state_store",
                    return_value=store,
                ) as state_factory,
                patch(
                    "backend.composition.control_mode.build_blob_store",
                    return_value=blobs,
                ) as blob_factory,
            ):
                app, _queue = build_control_app(
                    repo_root=None,
                    env=env,
                    execution_backend=FakeSandboxBackend(),
                )
            self.addCleanup(app.shutdown)

            self.assertEqual(
                app.workspace.repo_root, control_mode.CONTROL_COMPAT_REPO_ROOT
            )
            self.assertEqual(
                state_factory.call_args.kwargs["db_path"],
                control_mode.CONTROL_COMPAT_REPO_ROOT
                / ".research_plugin"
                / "state.sqlite",
            )
            self.assertEqual(
                blob_factory.call_args.kwargs["default_root"],
                control_mode.CONTROL_COMPAT_REPO_ROOT / ".research_plugin" / "blobs",
            )

    def test_control_server_reads_allowed_origins_from_env(self) -> None:
        from backend.composition.control_mode import build_control_server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = build_control_server(
                repo_root=root,
                env={
                    **_mounted_mgmt_key_env(root),
                    ALLOWED_ORIGINS_ENV_VAR: (
                        "https://ui.example.com, http://localhost:5173"
                    )
                },
            )
            self.addCleanup(server.shutdown)
            client = TestClient(server.fastapi_app, raise_server_exceptions=False)

            allowed = client.options(
                "/api/projects",
                headers={
                    "Origin": "https://ui.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
            self.assertEqual(
                allowed.headers.get("access-control-allow-origin"),
                "https://ui.example.com",
            )
            blocked = client.options(
                "/api/projects",
                headers={
                    "Origin": "https://evil.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
            self.assertNotEqual(
                blocked.headers.get("access-control-allow-origin"),
                "https://evil.example.com",
            )

    def test_control_server_warns_when_allowed_origins_empty(self) -> None:
        from backend.composition.control_mode import build_control_server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertLogs(
                "backend.composition.control_mode", level="WARNING"
            ) as logs:
                server = build_control_server(
                    repo_root=root,
                    env=_mounted_mgmt_key_env(root),
                )
            self.addCleanup(server.shutdown)
            self.assertIn(ALLOWED_ORIGINS_ENV_VAR, "\n".join(logs.output))

    def test_control_server_private_surface_and_cors_are_configured_independently(self) -> None:
        from backend.composition.control_mode import build_control_server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertNoLogs("backend.composition.control_mode", level="WARNING"):
                server = build_control_server(
                    repo_root=root,
                    env={
                        **_mounted_mgmt_key_env(root),
                        CONTROL_RESTRICT_CORS_ENV_VAR: "false",
                    },
                )
            self.addCleanup(server.shutdown)
            client = TestClient(server.fastapi_app, raise_server_exceptions=False)

            projects = client.get("/api/projects")
            self.assertEqual(projects.status_code, 200, projects.text)

            daemon_poll = client.get("/api/daemon/tasks?wait=0")
            self.assertEqual(daemon_poll.status_code, 200, daemon_poll.text)

            admin_cleanup = client.post("/api/admin/cleanup")
            self.assertEqual(admin_cleanup.status_code, 200, admin_cleanup.text)

            counters = client.get("/api/admin/tenants/local/counters")
            self.assertEqual(counters.status_code, 200, counters.text)

            old_daemon = client.get(
                "/api/daemon/tasks?wait=0",
                headers={CLIENT_VERSION_HEADER: "0.0001"},
            )
            self.assertEqual(old_daemon.status_code, 426, old_daemon.text)

            acme_project = server.app.projects.create(
                name="Acme Hosted", tenant_id="acme"
            )
            daemon_write = client.post(
                "/api/daemon/resources/observe",
                json={
                    "project_id": acme_project["id"],
                    "path": "results.txt",
                    "content_sha256": "0" * 64,
                    "mtime_ns": 1,
                    "ctime_ns": 1,
                    "size_bytes": 1,
                },
            )
            self.assertEqual(daemon_write.status_code, 200, daemon_write.text)

            meta = client.get("/api/meta")
            self.assertEqual(meta.status_code, 200, meta.text)
            body = meta.json()
            self.assertEqual(body["mode"], "control")
            self.assertTrue(body["capabilities"]["hosted_control"])
            self.assertFalse(body["capabilities"]["local_data_plane_http"])

            preflight = client.options(
                "/api/projects",
                headers={
                    "Origin": "https://dev.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
            self.assertEqual(
                preflight.headers.get("access-control-allow-origin"), "*"
            )


if __name__ == "__main__":
    unittest.main()
