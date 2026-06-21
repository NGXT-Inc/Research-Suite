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
    DB_URL_ENV_VAR,
    MGMT_KEY_PATH_ENV_VAR,
    MGMT_PUBLIC_KEY_ENV_VAR,
)
from backend.execution.backends.fake import FakeSandboxBackend
from backend.http_api import create_fastapi_app
from backend.state import StateStore
from backend.state.blobs import LocalDirBlobStore
from backend.state.managed_mgmt_keys import MountedMgmtKeyStore
from backend.utils import ValidationError


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
            app, _queue, auth = build_control_app(
                repo_root=root,
                env=_mounted_mgmt_key_env(root),
                execution_backend=FakeSandboxBackend(),
            )
            self.addCleanup(app.shutdown)
            token = auth.mint_token(tenant_id="acme")
            headers = {"Authorization": f"Bearer {token}"}
            client = TestClient(
                create_fastapi_app(app=app, auth=auth),
                raise_server_exceptions=False,
            )

            created = client.post(
                "/api/projects", json={"name": "Control Telemetry"}, headers=headers
            )
            self.assertEqual(created.status_code, 201, created.text)
            project_id = created.json()["id"]
            claim = client.post(
                f"/api/projects/{project_id}/claims",
                json={"statement": "A scoped control-plane claim."},
                headers=headers,
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
                headers=headers,
            )
            self.assertEqual(listed.status_code, 200, listed.text)
            calls = listed.json()["calls"]
            self.assertGreaterEqual(len(calls), 1)
            self.assertTrue(listed.json()["by_tool"])
            review_call = next(call for call in calls if call["tool"] == "review.start")
            detail = client.get(
                f"/api/debug/tool-calls/{review_call['id']}",
                headers=headers,
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["args"]["reviewer_capability"], "[redacted]")
            self.assertEqual(detail.json()["result"]["capability"], "[redacted]")
            activity = client.get("/api/activity", headers=headers)
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
            app, _queue, _auth = build_control_app(
                repo_root=root / "staging",
                env=env,
                execution_backend=FakeSandboxBackend(),
            )
            self.addCleanup(app.shutdown)

            self.assertIsInstance(app.sandboxes.mgmt_keys, MountedMgmtKeyStore)
            self.assertEqual(
                app.sandboxes.mgmt_keys.ensure(experiment_id="exp_1"),
                "ssh-ed25519 AAAAmanaged",
            )
            self.assertEqual(
                app.sandboxes.mgmt_keys.key_path(experiment_id="exp_1"), key_path
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
            app, _queue, _auth = build_control_app(
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
                app, _queue, _auth = build_control_app(
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


if __name__ == "__main__":
    unittest.main()
