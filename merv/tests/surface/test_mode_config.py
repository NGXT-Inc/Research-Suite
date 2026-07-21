from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from merv.brain.surface.config import (
    MGMT_KEY_PATH_ENV_VAR,
    MGMT_PUBLIC_KEY_ENV_VAR,
    Mode,
    STORAGE_ACCESS_KEY_ID_ENV_VAR,
    STORAGE_PROVIDER_ENV_VAR,
    STORAGE_SECRET_ACCESS_KEY_ENV_VAR,
    resolve_mode,
    resolve_storage_access_key_id,
    resolve_storage_provider,
    resolve_storage_secret_access_key,
    storage_feature_enabled,
)
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.surface.transport.http_api import create_fastapi_app
from merv.brain.surface.transport.http_policy import HttpSurfacePolicy
from merv.brain.kernel.utils import ValidationError


def _mounted_mgmt_key_env(root: Path) -> dict[str, str]:
    key_path = root / "managed_key"
    key_path.write_text("PRIVATE KEY\n", encoding="utf-8")
    key_path.chmod(0o600)
    return {
        MGMT_KEY_PATH_ENV_VAR: str(key_path),
        MGMT_PUBLIC_KEY_ENV_VAR: "ssh-ed25519 AAAAmanaged",
    }


def _hosted_surface() -> HttpSurfacePolicy:
    return HttpSurfacePolicy.for_surface(
        restrict_cors=True,
        hosted_control=True,
    )


class ModeConfigTest(unittest.TestCase):
    def test_default_is_local(self) -> None:
        self.assertIs(resolve_mode(env={}), Mode.LOCAL)

    def test_explicit_local(self) -> None:
        self.assertIs(resolve_mode(env={"RESEARCH_PLUGIN_MODE": "local"}), Mode.LOCAL)
        self.assertIs(resolve_mode(env={"RESEARCH_PLUGIN_MODE": " Local "}), Mode.LOCAL)

    def test_control_mode_is_runnable(self) -> None:
        self.assertIs(resolve_mode(env={"RESEARCH_PLUGIN_MODE": "control"}), Mode.CONTROL)

    def test_unknown_mode_fails(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            resolve_mode(env={"RESEARCH_PLUGIN_MODE": "cloud"})
        self.assertIn("unknown", ctx.exception.message)

    def test_daemon_mode_is_removed(self) -> None:
        with self.assertRaises(ValidationError):
            resolve_mode(env={"RESEARCH_PLUGIN_MODE": " Daemon "})


class StorageConfigTest(unittest.TestCase):
    def test_storage_is_disabled_unless_s3_provider_is_explicit(self) -> None:
        self.assertIsNone(resolve_storage_provider({}))
        self.assertFalse(storage_feature_enabled({}))
        self.assertEqual(
            resolve_storage_provider({STORAGE_PROVIDER_ENV_VAR: " s3 "}),
            "s3",
        )
        self.assertTrue(storage_feature_enabled({STORAGE_PROVIDER_ENV_VAR: "s3"}))
        with self.assertRaises(ValidationError):
            resolve_storage_provider({STORAGE_PROVIDER_ENV_VAR: "local"})

    def test_storage_access_key_prefers_storage_env_then_aws_then_none(self) -> None:
        self.assertEqual(
            resolve_storage_access_key_id(
                {
                    STORAGE_ACCESS_KEY_ID_ENV_VAR: " storage-ak ",
                    "AWS_ACCESS_KEY_ID": "aws-ak",
                }
            ),
            "storage-ak",
        )
        self.assertEqual(
            resolve_storage_access_key_id({"AWS_ACCESS_KEY_ID": " aws-ak "}),
            "aws-ak",
        )
        self.assertIsNone(resolve_storage_access_key_id({}))
        self.assertIsNone(
            resolve_storage_access_key_id(
                {STORAGE_ACCESS_KEY_ID_ENV_VAR: " ", "AWS_ACCESS_KEY_ID": " "}
            )
        )

    def test_storage_secret_prefers_storage_env_then_aws_then_none(self) -> None:
        self.assertEqual(
            resolve_storage_secret_access_key(
                {
                    STORAGE_SECRET_ACCESS_KEY_ENV_VAR: " storage-secret ",
                    "AWS_SECRET_ACCESS_KEY": "aws-secret",
                }
            ),
            "storage-secret",
        )
        self.assertEqual(
            resolve_storage_secret_access_key({"AWS_SECRET_ACCESS_KEY": " aws-secret "}),
            "aws-secret",
        )
        self.assertIsNone(resolve_storage_secret_access_key({}))
        self.assertIsNone(
            resolve_storage_secret_access_key(
                {STORAGE_SECRET_ACCESS_KEY_ENV_VAR: " ", "AWS_SECRET_ACCESS_KEY": " "}
            )
        )


class LocalModeParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(create_fastapi_app(self.app))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_token_needed_and_health_is_slim(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(set(health.json()), {"ok", "version"})

        project = self.client.post("/api/projects", json={"name": "Proj P"})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        claim = self.client.post(
            f"/api/projects/{project_id}/claims",
            json={"statement": "A representative claim for the local parity check."},
        )
        self.assertEqual(claim.status_code, 201, claim.text)
        home = self.client.get(f"/api/projects/{project_id}/home")
        self.assertEqual(home.status_code, 200, home.text)
        self.assertEqual(home.json()["project"]["id"], project_id)

    def test_local_mcp_rejects_repo_context_at_the_brain_boundary(self) -> None:
        project = self.client.post("/api/projects", json={"name": "Local MCP"})
        self.assertEqual(project.status_code, 201, project.text)
        claim = self.client.post(
            f"/api/projects/{project.json()['id']}/claims",
            json={"statement": "Local context scoping still works."},
        )
        self.assertEqual(claim.status_code, 201, claim.text)

        resp = self.client.post(
            "/mcp/call",
            json={
                "name": "claim.list",
                "arguments": {"project_id": project.json()["id"]},
                "context": {"repo_root": str(self.repo)},
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["reason"], "repo_root_hidden_from_cloud")


class HostedControlSurfaceTest(unittest.TestCase):
    LOCAL_RESPONSE_KEYS = {"repo_root", "local_sync_dir", "local_experiment_dir"}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(
            create_fastapi_app(self.app, surface_policy=_hosted_surface()),
            raise_server_exceptions=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def assertNoLocalDataPlaneFields(self, value) -> None:  # noqa: ANN001
        if isinstance(value, dict):
            leaked = self.LOCAL_RESPONSE_KEYS & set(value)
            self.assertFalse(leaked, f"leaked local data-plane fields: {sorted(leaked)}")
            for item in value.values():
                self.assertNoLocalDataPlaneFields(item)
        elif isinstance(value, list):
            for item in value:
                self.assertNoLocalDataPlaneFields(item)

    def test_private_control_needs_no_token_and_health_is_slim(self) -> None:
        projects = self.client.get("/api/projects")
        self.assertEqual(projects.status_code, 200, projects.text)

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertTrue(body["ok"])
        self.assertNotIn("repo_root", body)
        self.assertNotIn("store", body)

    def test_control_mcp_hides_and_rejects_data_plane_tools(self) -> None:
        tools = self.client.get("/mcp/tools")
        self.assertEqual(tools.status_code, 200, tools.text)
        names = {tool["name"] for tool in tools.json()["tools"]}
        self.assertIn("claim.create", names)
        self.assertNotIn("resource.register", names)
        self.assertNotIn("feed.post", names)

        rejected = self.client.post(
            "/mcp/call",
            json={"name": "resource.register", "arguments": {"path": "x.txt"}},
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual(rejected.json()["error_code"], "data_plane_required")

    def test_control_rejects_repo_root_context(self) -> None:
        project = self.client.post(
            "/api/projects",
            json={"name": "No Repo Root Context"},
        )
        self.assertEqual(project.status_code, 201, project.text)

        resp = self.client.post(
            "/mcp/call",
            json={
                "name": "claim.list",
                "arguments": {"project_id": project.json()["id"]},
                "context": {"repo_root": str(self.repo)},
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["reason"], "repo_root_hidden_from_cloud")

    def test_hosted_sandbox_get_addresses_by_sandbox_uid(self) -> None:
        # Decoupled identity: a sandbox is found by its durable sandbox_uid, not
        # by experiment. When one experiment drives several sandboxes, hosted
        # sandbox.get(sandbox_uid=...) must return THAT sandbox, not the primary.
        project = self.client.post("/api/projects", json={"name": "Multi Sandbox"})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        exp_id = self.app.call_tool(
            name="experiment.create",
            arguments={"project_id": project_id, "name": "multi", "intent": "two boxes"},
        )["id"]
        backend = self.app.execution_backend
        backend.alive["sbx_primary"] = True
        backend.alive["sbx_extra"] = True
        for uid, sid in (("uid_primary", "sbx_primary"), ("uid_extra", "sbx_extra")):
            self.app.sandboxes.registry.upsert(
                experiment_id=exp_id,
                sandbox_uid=uid,
                project_id=project_id,
                status="running",
                sandbox_id=sid,
            )

        def _get(**extra):
            resp = self.client.post(
                "/mcp/call",
                json={
                    "name": "sandbox.get",
                    "arguments": {"project_id": project_id, "experiment_id": exp_id, **extra},
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            return resp.json()["result"]

        primary = _get(sandbox_uid="uid_primary")
        extra = _get(sandbox_uid="uid_extra")
        self.assertEqual(primary["sandbox_id"], "sbx_primary")
        self.assertEqual(extra["sandbox_id"], "sbx_extra")
        # The two uids must not collapse to the same sandbox (the hosted bug).
        self.assertNotEqual(primary["sandbox_id"], extra["sandbox_id"])

    def test_data_plane_http_mutation_route_is_deleted(self) -> None:
        project = self.client.post("/api/projects", json={"name": "Hosted Project"})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]

        resp = self.client.post(
            f"/api/projects/{project_id}/resources",
            json={"path": "local-result.json", "kind": "result"},
        )
        self.assertEqual(resp.status_code, 405, resp.text)

    def test_hosted_resource_content_does_not_read_local_checkout(self) -> None:
        project = self.client.post("/api/projects", json={"name": "Hosted Content"})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        (self.repo / "results.json").write_text('{"acc": 0.9}', encoding="utf-8")
        resource = self.app.call_tool(
            name="resource.register",
            arguments={
                "project_id": project_id,
                "path": "results.json",
                "kind": "result",
            },
        )

        content = self.client.get(
            f"/api/projects/{project_id}/resources/{resource['id']}/content"
        )
        self.assertEqual(content.status_code, 200, content.text)
        self.assertFalse(content.json()["available"])
        self.assertNoLocalDataPlaneFields(content.json())

    def test_admin_cleanup_runs_on_private_control_surface(self) -> None:
        class _Report:
            def as_dict(self):  # noqa: ANN001
                return {"ok": True}

        class _Cleanup:
            def __init__(self) -> None:
                self.calls = 0

            def run_all(self):  # noqa: ANN001
                self.calls += 1
                return _Report()

        cleanup = _Cleanup()
        counter_calls = []

        def tenant_counters(*, tenant_id):  # noqa: ANN001
            counter_calls.append(tenant_id)
            return {"tenant_id": tenant_id, "tool_calls": 7}

        client = TestClient(
            create_fastapi_app(
                self.app,
                surface_policy=_hosted_surface(),
                cleanup=cleanup,
                tenant_counters=tenant_counters,
            ),
            raise_server_exceptions=False,
        )

        ok = client.post("/api/admin/cleanup")
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["cleaned"], {"ok": True})
        self.assertEqual(cleanup.calls, 1)
        counters = client.get("/api/admin/tenants/acme/counters")
        self.assertEqual(counters.status_code, 200, counters.text)
        self.assertEqual(counters.json(), {"tenant_id": "acme", "tool_calls": 7})
        self.assertEqual(counter_calls, ["acme"])

    def test_data_plane_submission_endpoint_is_private_but_not_token_gated(self) -> None:
        client = TestClient(
            create_fastapi_app(
                self.app,
                surface_policy=_hosted_surface(),
            ),
            raise_server_exceptions=False,
        )

        project = self.app.projects.list_projects()["projects"][0]
        observed = client.post(
            "/api/data-plane/resources/observe",
            json={
                "project_id": project["id"],
                "path": "results.txt",
                "content_sha256": "0" * 64,
                "mtime_ns": 1,
                "ctime_ns": 1,
                "size_bytes": 1,
            },
        )
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(observed.json()["path"], "results.txt")


class SecretStoreCredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        import os

        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.tmp.name) / ".env"
        self.env_file.write_text("MODAL_TOKEN_ID=from_dotenv\n", encoding="utf-8")
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "RESEARCH_PLUGIN_MODE",
                "RESEARCH_PLUGIN_MODAL_ENV_FILE",
                "MODAL_TOKEN_ID",
            )
        }
        os.environ.pop("MODAL_TOKEN_ID", None)

    def tearDown(self) -> None:
        import os

        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_explicit_env_file_is_the_secret_store_seam_in_control(self) -> None:
        import os

        from merv.brain.sandbox.execution.backends.modal.config import load_modal_env_file

        os.environ["RESEARCH_PLUGIN_MODE"] = "control"
        os.environ["RESEARCH_PLUGIN_MODAL_ENV_FILE"] = str(self.env_file)
        load_modal_env_file()
        self.assertEqual(os.environ.get("MODAL_TOKEN_ID"), "from_dotenv")

    def test_implicit_dotenv_disabled_in_control(self) -> None:
        import os

        from merv.brain.sandbox.execution.backends.modal import config as modal_config

        os.environ["RESEARCH_PLUGIN_MODE"] = "control"
        os.environ.pop("RESEARCH_PLUGIN_MODAL_ENV_FILE", None)
        with patch.object(
            modal_config.Path, "exists", return_value=True
        ), patch.object(
            modal_config.Path, "read_text", return_value="MODAL_TOKEN_ID=leak\n"
        ):
            modal_config.load_modal_env_file()
        self.assertIsNone(os.environ.get("MODAL_TOKEN_ID"))

    def test_implicit_dotenv_still_works_in_local(self) -> None:
        import os

        from merv.brain.sandbox.execution.backends.modal import config as modal_config

        os.environ["RESEARCH_PLUGIN_MODE"] = "local"
        os.environ.pop("RESEARCH_PLUGIN_MODAL_ENV_FILE", None)
        with patch.object(
            modal_config.Path, "exists", return_value=True
        ), patch.object(
            modal_config.Path, "read_text", return_value="MODAL_TOKEN_ID=local_ok\n"
        ):
            modal_config.load_modal_env_file()
        self.assertEqual(os.environ.get("MODAL_TOKEN_ID"), "local_ok")


class VersionHandshakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.client = TestClient(
            create_fastapi_app(self.app, surface_policy=_hosted_surface()),
            raise_server_exceptions=False,
        )
        self.local_client = TestClient(
            create_fastapi_app(self.app), raise_server_exceptions=False
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_meta_returns_server_version_floors_and_capabilities(self) -> None:
        from merv.brain.kernel.version import (
            MIN_PROXY_VERSION,
            SERVER_VERSION,
        )

        control = self.client.get("/api/meta")
        self.assertEqual(control.status_code, 200, control.text)
        body = control.json()
        self.assertEqual(body["server_version"], SERVER_VERSION)
        self.assertNotIn("min_daemon_version", body)
        self.assertEqual(body["min_proxy_version"], MIN_PROXY_VERSION)
        self.assertEqual(body["mode"], "control")
        self.assertTrue(body["capabilities"]["hosted_control"])
        self.assertFalse(body["capabilities"]["local_data_plane_http"])

        local = self.local_client.get("/api/meta")
        self.assertEqual(local.status_code, 200, local.text)
        self.assertEqual(local.json()["mode"], "local")
        self.assertFalse(local.json()["capabilities"]["local_data_plane_http"])

    def test_in_range_client_passes_and_below_floor_is_rejected(self) -> None:
        from merv.brain.kernel.version import SERVER_VERSION

        ok = self.client.get(
            "/api/projects",
            headers={"X-RP-Client-Version": SERVER_VERSION},
        )
        self.assertEqual(ok.status_code, 200, ok.text)

        old = self.client.get(
            "/api/projects",
            headers={"X-RP-Client-Version": "0.0001"},
        )
        self.assertEqual(old.status_code, 426, old.text)
        self.assertEqual(old.json()["error_code"], "client_too_old")

    def test_local_mode_never_enforces_floor(self) -> None:
        resp = self.local_client.get(
            "/api/projects", headers={"X-RP-Client-Version": "0.0001"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_proxy_header_literal_matches_backend_constant(self) -> None:
        from merv.brain.kernel.version import CLIENT_VERSION_HEADER
        from tests.paths import PROXY_ROOT

        credential_src = (PROXY_ROOT / "credential_provider.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'"{CLIENT_VERSION_HEADER}"', credential_src)


class ModeCompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_control_server_builds_private_surface_and_data_plane_submission(self) -> None:
        from merv.brain.surface.composition import build_control_server

        server = build_control_server(
            repo_root=self.repo,
            env=_mounted_mgmt_key_env(self.repo),
        )
        self.addCleanup(server.shutdown)
        paths = {getattr(r, "path", "") for r in server.fastapi_app.routes}
        self.assertIn("/api/data-plane/resources/observe", paths)
        self.assertNotIn("/api/daemon/tasks", paths)
        self.assertIn("/mcp/call", paths)
        client = TestClient(server.fastapi_app, raise_server_exceptions=False)
        self.assertEqual(client.get("/api/projects").status_code, 200)

    def test_daemon_builder_is_removed(self) -> None:
        import merv.brain.surface.composition as composition

        self.assertFalse(hasattr(composition, "build_daemon_server"))


if __name__ == "__main__":
    unittest.main()
