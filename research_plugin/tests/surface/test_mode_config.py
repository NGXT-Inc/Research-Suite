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

    def test_planned_modes_fail_with_not_implemented_message(self) -> None:
        for planned in ("control", "daemon"):
            with self.subTest(mode=planned):
                with self.assertRaises(ValidationError) as ctx:
                    resolve_mode(env={"RESEARCH_PLUGIN_MODE": planned})
                self.assertIn("not implemented", ctx.exception.message)

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


if __name__ == "__main__":
    unittest.main()
