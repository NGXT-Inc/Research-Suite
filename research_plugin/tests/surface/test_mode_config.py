from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import ResearchPluginApp
from backend.config import Mode, resolve_auth_required, resolve_mode
from backend.execution.backends.fake import FakeSandboxBackend
from backend.http_api import create_fastapi_app
from backend.services.identity import AuthService
from backend.utils import ValidationError
from tests.fakes import FakeRsyncSyncer


class ModeConfigTest(unittest.TestCase):
    def test_default_is_local(self) -> None:
        self.assertIs(resolve_mode(env={}), Mode.LOCAL)

    def test_explicit_local(self) -> None:
        self.assertIs(resolve_mode(env={"RESEARCH_PLUGIN_MODE": "local"}), Mode.LOCAL)
        self.assertIs(resolve_mode(env={"RESEARCH_PLUGIN_MODE": " Local "}), Mode.LOCAL)

    def test_control_and_daemon_modes_are_runnable(self) -> None:
        # Phase 8: control and daemon were stubbed as NotImplementedError in
        # Phase 0; now resolve_mode parses them to their Mode enum (the
        # compositions are real). Mode-specific fail-fast (daemon needs a
        # control URL) lives in the composition roots, not here.
        self.assertIs(resolve_mode(env={"RESEARCH_PLUGIN_MODE": "control"}), Mode.CONTROL)
        self.assertIs(resolve_mode(env={"RESEARCH_PLUGIN_MODE": " Daemon "}), Mode.DAEMON)

    def test_unknown_mode_fails(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            resolve_mode(env={"RESEARCH_PLUGIN_MODE": "cloud"})
        self.assertIn("unknown", ctx.exception.message)

    def test_auth_off_is_the_default(self) -> None:
        # The Phase 7 default: auth is off (local mode), so every existing
        # caller of create_fastapi_app (which passes no AuthService) is unchanged.
        self.assertFalse(resolve_auth_required(env={}))


class LocalModeAuthParityTest(unittest.TestCase):
    """An app built WITHOUT an AuthService behaves exactly as before Phase 7."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.client = TestClient(create_fastapi_app(self.app))  # no auth=

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_token_needed_and_health_is_rich(self) -> None:
        # No Authorization header, yet a representative tool sequence works and
        # /health still carries the local-mode detail (repo_root etc.).
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertIn("repo_root", health.json())

        project = self.client.post("/api/projects", json={"name": "P"})
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


class ControlModeAuthTest(unittest.TestCase):
    """With an AuthService injected: bearer auth required, /health slimmed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.auth = AuthService(store=self.app.store)
        self.token = self.auth.mint_token(tenant_id="acme")
        self.client = TestClient(
            create_fastapi_app(self.app, auth=self.auth),
            raise_server_exceptions=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_token_is_401(self) -> None:
        resp = self.client.get("/api/projects")
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_invalid_token_is_401(self) -> None:
        resp = self.client.get(
            "/api/projects", headers={"Authorization": "Bearer rpt_nope"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_passes(self) -> None:
        resp = self.client.get(
            "/api/projects", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_health_is_slim_and_leaks_no_paths(self) -> None:
        # /health is unauthenticated liveness, but must not leak host paths.
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertNotIn("repo_root", body)
        self.assertNotIn("store", body)

    def test_cors_allows_authorization_header(self) -> None:
        resp = self.client.options(
            "/api/projects",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        # Preflight is never auth-challenged; Authorization is an allowed header.
        allow = resp.headers.get("access-control-allow-headers", "")
        self.assertIn("Authorization", allow)


class ModeCompositionTest(unittest.TestCase):
    """The three composition roots build (or fail-fast) as the matrix says."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_control_server_builds_with_auth_and_daemon_endpoints(self) -> None:
        from backend.composition import build_control_server

        server = build_control_server(repo_root=self.repo)
        self.addCleanup(server.shutdown)
        # Auth ON (control mode) and the daemon task/sync-target endpoints exist.
        paths = {getattr(r, "path", "") for r in server.fastapi_app.routes}
        self.assertIn("/api/daemon/tasks", paths)
        self.assertIn("/api/daemon/sync-targets", paths)
        self.assertIn("/mcp/call", paths)
        # A valid token is required: the AuthService can mint one.
        token = server.auth.mint_token(tenant_id="acme")
        client = TestClient(server.fastapi_app, raise_server_exceptions=False)
        unauth = client.get("/api/projects")
        self.assertEqual(unauth.status_code, 401)
        ok = client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_daemon_refuses_to_start_without_control_url(self) -> None:
        from backend.composition import build_daemon_server

        with self.assertRaises(ValidationError) as ctx:
            build_daemon_server(control_url=None)
        self.assertIn("RESEARCH_PLUGIN_CONTROL_URL", ctx.exception.message)

    def test_local_app_builder_is_a_plain_research_plugin_app(self) -> None:
        from backend.composition import build_local_app

        app = build_local_app(
            repo_root=self.repo, db_path=self.repo / ".research_plugin" / "state.sqlite"
        )
        self.addCleanup(app.shutdown)
        self.assertIsInstance(app, ResearchPluginApp)


if __name__ == "__main__":
    unittest.main()
