from __future__ import annotations

import base64
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

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100ffff03000006000557bff8a40000000049454e44ae426082"
)


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


class ControlModeAuthTest(unittest.TestCase):
    """With an AuthService injected: bearer auth required, /health slimmed."""

    LOCAL_RESPONSE_KEYS = {"repo_root", "local_sync_dir", "local_experiment_dir"}

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
        self.other_token = self.auth.mint_token(tenant_id="other")
        self.daemon_token = self.auth.mint_token(
            tenant_id="acme", client_id="daemon", label="daemon"
        )
        self.other_daemon_token = self.auth.mint_token(
            tenant_id="other", client_id="daemon", label="other-daemon"
        )
        self.client = TestClient(
            create_fastapi_app(self.app, auth=self.auth),
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

    def test_cors_allows_spa_request_headers(self) -> None:
        resp = self.client.options(
            "/api/projects",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, X-RP-Client-Version",
            },
        )
        # Preflight is never auth-challenged; the SPA's auth + version headers
        # must be allowed before any observer/admin endpoint can be reached.
        allow = resp.headers.get("access-control-allow-headers", "").lower()
        self.assertIn("authorization", allow)
        self.assertIn("x-rp-client-version", allow)

    def test_data_plane_http_mutation_is_rejected_in_control_mode(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        project = self.client.post(
            "/api/projects", json={"name": "Hosted Project"}, headers=headers
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]

        resp = self.client.post(
            f"/api/projects/{project_id}/resources",
            json={"path": "local-result.json", "kind": "result"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body["error_code"], "data_plane_required")
        self.assertEqual(body["tool"], "resource.register_file")

    def test_hosted_release_skips_final_pull(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        project = self.client.post(
            "/api/projects", json={"name": "Hosted Release"}, headers=headers
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        exp = self.client.post(
            f"/api/projects/{project_id}/experiments",
            json={"name": "exp", "intent": "run"},
            headers=headers,
        )
        self.assertEqual(exp.status_code, 201, exp.text)
        exp_id = exp.json()["id"]

        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=project_id,
            sandbox_id="sb-hosted-release",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            expires_at="2999-01-01T00:00:00Z",
        )
        self.app.execution_backend.alive["sb-hosted-release"] = True
        self.app.execution_backend.by_experiment[exp_id] = "sb-hosted-release"
        called = False
        original = self.app.sandboxes._final_pull_row

        def _record_final_pull(*, row):  # noqa: ANN001
            nonlocal called
            called = True
            return original(row=row)

        self.app.sandboxes._final_pull_row = _record_final_pull
        try:
            resp = self.client.post(
                f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/release",
                headers=headers,
            )
        finally:
            self.app.sandboxes._final_pull_row = original

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "terminated")
        self.assertTrue(body["final_pull_skipped"])
        self.assertFalse(body["daemon_unreachable"])
        self.assertFalse(called)
        self.assertIn("sb-hosted-release", self.app.execution_backend.terminated)

    def test_hosted_http_sandbox_views_hide_local_data_plane_fields(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        project = self.client.post(
            "/api/projects", json={"name": "Hosted View"}, headers=headers
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        exp = self.client.post(
            f"/api/projects/{project_id}/experiments",
            json={"name": "exp-view", "intent": "run"},
            headers=headers,
        )
        self.assertEqual(exp.status_code, 201, exp.text)
        exp_id = exp.json()["id"]
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=project_id,
            sandbox_id="sb-hosted-view",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            sync_dir="/workspace/exp-view",
            workdir="/workspace/exp-view",
            expires_at="2999-01-01T00:00:00Z",
        )

        sandbox = self.client.get(
            f"/api/projects/{project_id}/experiments/{exp_id}/sandbox",
            headers=headers,
        )
        self.assertEqual(sandbox.status_code, 200, sandbox.text)
        sandboxes = self.client.get(
            f"/api/projects/{project_id}/sandboxes",
            headers=headers,
        )
        self.assertEqual(sandboxes.status_code, 200, sandboxes.text)
        home = self.client.get(f"/api/projects/{project_id}/home", headers=headers)
        self.assertEqual(home.status_code, 200, home.text)
        project_status = self.client.get(
            f"/api/projects/{project_id}/status", headers=headers
        )
        self.assertEqual(project_status.status_code, 200, project_status.text)
        experiment_status = self.client.get(
            f"/api/projects/{project_id}/experiments/{exp_id}/status",
            headers=headers,
        )
        self.assertEqual(experiment_status.status_code, 200, experiment_status.text)
        release = self.client.post(
            f"/api/projects/{project_id}/experiments/{exp_id}/sandbox/release",
            headers=headers,
        )
        self.assertEqual(release.status_code, 200, release.text)
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            project_id=project_id,
            sandbox_id="sb-hosted-view-2",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            sync_dir="/workspace/exp-view",
            workdir="/workspace/exp-view",
            expires_at="2999-01-01T00:00:00Z",
        )
        mcp_release = self.client.post(
            "/mcp/call",
            json={
                "name": "sandbox.release",
                "arguments": {"project_id": project_id, "experiment_id": exp_id},
            },
            headers=headers,
        )
        self.assertEqual(mcp_release.status_code, 200, mcp_release.text)
        activity = self.client.get(
            f"/api/activity?project_id={project_id}", headers=headers
        )
        self.assertEqual(activity.status_code, 200, activity.text)

        self.assertNoLocalDataPlaneFields(sandbox.json())
        self.assertNoLocalDataPlaneFields(sandboxes.json())
        self.assertNoLocalDataPlaneFields(home.json())
        self.assertNoLocalDataPlaneFields(project_status.json())
        self.assertNoLocalDataPlaneFields(experiment_status.json())
        self.assertNoLocalDataPlaneFields(release.json())
        self.assertNoLocalDataPlaneFields(mcp_release.json())
        self.assertNoLocalDataPlaneFields(activity.json())
        self.assertNotIn("activity_log", activity.json())

    def test_daemon_resource_endpoint_requires_daemon_token(self) -> None:
        from backend.dataplane.http_channel import HttpTaskQueue

        daemon_client = TestClient(
            create_fastapi_app(self.app, auth=self.auth, task_queue=HttpTaskQueue()),
            raise_server_exceptions=False,
        )
        headers = {"Authorization": f"Bearer {self.token}"}
        project = self.client.post(
            "/api/projects", json={"name": "Hosted Project"}, headers=headers
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        payload = {
            "project_id": project_id,
            "path": "daemon/result.txt",
            "kind": "result",
            "mtime_ns": 1,
            "ctime_ns": 1,
            "size_bytes": 3,
            "content_sha256": "0" * 64,
            "content_type": "text/plain",
        }

        denied = daemon_client.post(
            "/api/daemon/resources/observe", json=payload, headers=headers
        )
        self.assertEqual(denied.status_code, 400, denied.text)
        self.assertEqual(denied.json()["error_code"], "permission_denied")

        ok = daemon_client.post(
            "/api/daemon/resources/observe",
            json=payload,
            headers={"Authorization": f"Bearer {self.daemon_token}"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["path"], "daemon/result.txt")

    def test_daemon_resource_endpoint_is_tenant_scoped(self) -> None:
        from backend.dataplane.http_channel import HttpTaskQueue

        daemon_client = TestClient(
            create_fastapi_app(self.app, auth=self.auth, task_queue=HttpTaskQueue()),
            raise_server_exceptions=False,
        )
        project = self.client.post(
            "/api/projects",
            json={"name": "Tenant Scoped"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        payload = {
            "project_id": project.json()["id"],
            "path": "daemon/result.txt",
            "kind": "result",
            "mtime_ns": 1,
            "ctime_ns": 1,
            "size_bytes": 3,
            "content_sha256": "0" * 64,
            "content_type": "text/plain",
        }

        denied = daemon_client.post(
            "/api/daemon/resources/observe",
            json=payload,
            headers={"Authorization": f"Bearer {self.other_daemon_token}"},
        )
        self.assertEqual(denied.status_code, 404, denied.text)

        ok = daemon_client.post(
            "/api/daemon/resources/observe",
            json=payload,
            headers={"Authorization": f"Bearer {self.daemon_token}"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_daemon_associate_validates_intent_before_decoding_bytes(self) -> None:
        from backend.dataplane.http_channel import HttpTaskQueue

        daemon_client = TestClient(
            create_fastapi_app(self.app, auth=self.auth, task_queue=HttpTaskQueue()),
            raise_server_exceptions=False,
        )
        project = self.client.post(
            "/api/projects",
            json={"name": "Assoc Guard"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]

        resp = daemon_client.post(
            "/api/daemon/resources/associate",
            json={
                "project_id": project_id,
                "resource_id": "res_missing",
                "target_type": "experiment",
                "target_id": "exp_missing",
                "role": "not-a-valid-role",
                "blob": {"data_b64": "not base64 !!!"},
            },
            headers={"Authorization": f"Bearer {self.daemon_token}"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertNotIn("base64", resp.json()["detail"].lower())

    def test_daemon_feed_post_endpoint_requires_daemon_token_and_tenant(self) -> None:
        from backend.dataplane.http_channel import HttpTaskQueue

        daemon_client = TestClient(
            create_fastapi_app(self.app, auth=self.auth, task_queue=HttpTaskQueue()),
            raise_server_exceptions=False,
        )
        project = self.client.post(
            "/api/projects",
            json={"name": "Feed Daemon"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        self.app.feed.register(project_id=project_id, handle="Nova-7")
        payload = {
            "project_id": project_id,
            "handle": "Nova-7",
            "text": "daemon image",
            "image": {
                "path": "plot.png",
                "data_b64": base64.b64encode(_PNG).decode("ascii"),
            },
        }

        denied = daemon_client.post(
            "/api/daemon/feed/post",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(denied.status_code, 400, denied.text)
        self.assertEqual(denied.json()["error_code"], "permission_denied")

        foreign = daemon_client.post(
            "/api/daemon/feed/post",
            json=payload,
            headers={"Authorization": f"Bearer {self.other_daemon_token}"},
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)

        ok = daemon_client.post(
            "/api/daemon/feed/post",
            json=payload,
            headers={"Authorization": f"Bearer {self.daemon_token}"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        post_id = ok.json()["post"]["id"]
        data, ctype = self.app.feed.get_image(project_id=project_id, post_id=post_id)
        self.assertEqual(data, _PNG)
        self.assertEqual(ctype, "image/png")

    def test_daemon_feed_validates_intent_before_decoding_image(self) -> None:
        from backend.dataplane.http_channel import HttpTaskQueue

        daemon_client = TestClient(
            create_fastapi_app(self.app, auth=self.auth, task_queue=HttpTaskQueue()),
            raise_server_exceptions=False,
        )
        project = self.client.post(
            "/api/projects",
            json={"name": "Feed Byte Guard"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(project.status_code, 201, project.text)

        resp = daemon_client.post(
            "/api/daemon/feed/post",
            json={
                "project_id": project.json()["id"],
                "handle": "Ghost",
                "text": "bad intent",
                "image": {"path": "plot.png", "data_b64": "not base64 !!!"},
            },
            headers={"Authorization": f"Bearer {self.daemon_token}"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertNotIn("base64", resp.json()["detail"].lower())

    def test_daemon_feed_rejects_oversized_encoded_image_before_decode(self) -> None:
        from unittest.mock import patch

        from backend.dataplane.http_channel import HttpTaskQueue

        daemon_client = TestClient(
            create_fastapi_app(self.app, auth=self.auth, task_queue=HttpTaskQueue()),
            raise_server_exceptions=False,
        )
        project = self.client.post(
            "/api/projects",
            json={"name": "Feed Size Guard"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        self.app.feed.register(project_id=project_id, handle="Nova-7")

        with patch("backend.http_api.MAX_IMAGE_BYTES", 3):
            resp = daemon_client.post(
                "/api/daemon/feed/post",
                json={
                    "project_id": project_id,
                    "handle": "Nova-7",
                    "text": "too large",
                    "image": {"path": "plot.png", "data_b64": "AAAAAAAA"},
                },
                headers={"Authorization": f"Bearer {self.daemon_token}"},
            )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("byte limit", resp.json()["detail"])

    def test_feed_http_reads_are_tenant_scoped(self) -> None:
        project = self.client.post(
            "/api/projects",
            json={"name": "Feed Read Tenant"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        self.app.feed.register(project_id=project_id, handle="Nova-7")
        post = self.app.feed.post_observed(
            project_id=project_id,
            handle="Nova-7",
            text="private plot",
            image_path="plot.png",
            image_bytes=_PNG,
        )["post"]

        denied_list = self.client.get(
            f"/api/projects/{project_id}/feed",
            headers={"Authorization": f"Bearer {self.other_token}"},
        )
        self.assertEqual(denied_list.status_code, 404, denied_list.text)

        denied_image = self.client.get(
            f"/api/projects/{project_id}/feed/{post['id']}/image",
            headers={"Authorization": f"Bearer {self.other_token}"},
        )
        self.assertEqual(denied_image.status_code, 404, denied_image.text)

        ok_list = self.client.get(
            f"/api/projects/{project_id}/feed",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(ok_list.status_code, 200, ok_list.text)
        self.assertEqual(ok_list.json()["posts"][0]["id"], post["id"])

        ok_image = self.client.get(
            f"/api/projects/{project_id}/feed/{post['id']}/image",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(ok_image.status_code, 200, ok_image.text)
        self.assertEqual(ok_image.content, _PNG)

    def test_daemon_tasks_are_tenant_scoped(self) -> None:
        from backend.dataplane.http_channel import HttpTaskQueue

        queue = HttpTaskQueue()
        queue.enqueue(
            task_type="final_pull",
            payload={"session": {"experiment_id": "exp_a"}, "name": "exp-a"},
            tenant_id="acme",
        )
        daemon_client = TestClient(
            create_fastapi_app(self.app, auth=self.auth, task_queue=queue),
            raise_server_exceptions=False,
        )

        other_poll = daemon_client.get(
            "/api/daemon/tasks?wait=0",
            headers={"Authorization": f"Bearer {self.other_daemon_token}"},
        )
        self.assertEqual(other_poll.status_code, 200, other_poll.text)
        self.assertIsNone(other_poll.json()["task"])

        acme_poll = daemon_client.get(
            "/api/daemon/tasks?wait=0",
            headers={"Authorization": f"Bearer {self.daemon_token}"},
        )
        self.assertEqual(acme_poll.status_code, 200, acme_poll.text)
        task_id = acme_poll.json()["task"]["id"]

        denied_ack = daemon_client.post(
            f"/api/daemon/tasks/{task_id}/ack",
            json={"ok": True},
            headers={"Authorization": f"Bearer {self.other_daemon_token}"},
        )
        self.assertEqual(denied_ack.status_code, 400, denied_ack.text)
        self.assertEqual(denied_ack.json()["error_code"], "permission_denied")

        ok_ack = daemon_client.post(
            f"/api/daemon/tasks/{task_id}/ack",
            json={"ok": True, "result": {"ok": True}},
            headers={"Authorization": f"Bearer {self.daemon_token}"},
        )
        self.assertEqual(ok_ack.status_code, 200, ok_ack.text)

    def test_daemon_sync_targets_are_tenant_scoped(self) -> None:
        from backend.dataplane.http_channel import HttpTaskQueue

        other_project = self.app.projects.create(
            name="Other Tenant", tenant_id="other"
        )
        project = self.client.post(
            "/api/projects",
            json={"name": "Sync Target Tenant"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        acme_project_id = project.json()["id"]
        self.app.sandboxes.registry.upsert(
            experiment_id="exp_acme",
            project_id=acme_project_id,
            status="running",
            sandbox_id="sbx_acme",
            ssh_host="127.0.0.1",
            ssh_port=22,
            ssh_user="root",
            sync_dir="/remote/acme",
            sandbox_data_dir="/remote/acme/data",
        )
        self.app.sandboxes.registry.upsert(
            experiment_id="exp_other",
            project_id=other_project["id"],
            status="running",
            sandbox_id="sbx_other",
            ssh_host="127.0.0.1",
            ssh_port=22,
            ssh_user="root",
            sync_dir="/remote/other",
            sandbox_data_dir="/remote/other/data",
        )
        daemon_client = TestClient(
            create_fastapi_app(
                self.app,
                auth=self.auth,
                task_queue=HttpTaskQueue(),
                sync_targets_source=self.app.sandboxes.control_view,
            ),
            raise_server_exceptions=False,
        )

        acme = daemon_client.get(
            "/api/daemon/sync-targets",
            headers={"Authorization": f"Bearer {self.daemon_token}"},
        )
        self.assertEqual(acme.status_code, 200, acme.text)
        self.assertEqual(
            {target["experiment_id"] for target in acme.json()["targets"]},
            {"exp_acme"},
        )

        other = daemon_client.get(
            "/api/daemon/sync-targets",
            headers={"Authorization": f"Bearer {self.other_daemon_token}"},
        )
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(
            {target["experiment_id"] for target in other.json()["targets"]},
            {"exp_other"},
        )

    def test_control_mcp_catalog_hides_data_plane_tools(self) -> None:
        resp = self.client.get(
            "/mcp/tools", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        names = {tool["name"] for tool in resp.json()["tools"]}
        self.assertNotIn("resource.register_file", names)
        self.assertNotIn("sandbox.request", names)
        self.assertNotIn("feed.post", names)
        self.assertIn("claim.create", names)

    def test_data_plane_mcp_tool_is_rejected_in_control_mode(self) -> None:
        resp = self.client.post(
            "/mcp/call",
            json={"name": "resource.register_file", "arguments": {"path": "x.txt"}},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error_code"], "data_plane_required")

        feed = self.client.post(
            "/mcp/call",
            json={"name": "feed.post", "arguments": {"handle": "Nova-7", "text": "x"}},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(feed.status_code, 400, feed.text)
        self.assertEqual(feed.json()["error_code"], "data_plane_required")

    def test_mcp_sandbox_get_is_tenant_scoped(self) -> None:
        project = self.client.post(
            "/api/projects",
            json={"name": "Sandbox Tenant"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        self.app.sandboxes.registry.upsert(
            experiment_id="exp_acme",
            project_id=project_id,
            status="failed",
            sandbox_id="sbx_acme",
            workdir="/workspace/experiments/acme",
            sync_dir="/workspace/experiments/acme",
        )

        denied = self.client.post(
            "/mcp/call",
            json={
                "name": "sandbox.get",
                "arguments": {"project_id": project_id, "experiment_id": "exp_acme"},
            },
            headers={"Authorization": f"Bearer {self.other_token}"},
        )
        self.assertEqual(denied.status_code, 404, denied.text)

        ok = self.client.post(
            "/mcp/call",
            json={
                "name": "sandbox.get",
                "arguments": {"project_id": project_id, "experiment_id": "exp_acme"},
            },
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["result"]["experiment_id"], "exp_acme")


class SecretStoreCredentialsTest(unittest.TestCase):
    """Control mode disables user-machine .env discovery (cloud plan Phase 9)."""

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

        from backend.execution.backends.modal.config import load_modal_env_file

        os.environ["RESEARCH_PLUGIN_MODE"] = "control"
        os.environ["RESEARCH_PLUGIN_MODAL_ENV_FILE"] = str(self.env_file)
        load_modal_env_file()
        # An EXPLICIT env file (a mounted secret) is still honored in control.
        self.assertEqual(os.environ.get("MODAL_TOKEN_ID"), "from_dotenv")

    def test_implicit_dotenv_disabled_in_control(self) -> None:
        import os
        from unittest.mock import patch

        from backend.execution.backends.modal import config as modal_config

        os.environ["RESEARCH_PLUGIN_MODE"] = "control"
        os.environ.pop("RESEARCH_PLUGIN_MODAL_ENV_FILE", None)
        # Point the implicit package-root .env at our fixture; control mode must
        # NOT read it (only an explicit env file is honored).
        with patch.object(
            modal_config.Path, "exists", return_value=True
        ), patch.object(
            modal_config.Path, "read_text", return_value="MODAL_TOKEN_ID=leak\n"
        ):
            modal_config.load_modal_env_file()
        self.assertIsNone(os.environ.get("MODAL_TOKEN_ID"))

    def test_implicit_dotenv_still_works_in_local(self) -> None:
        import os
        from unittest.mock import patch

        from backend.execution.backends.modal import config as modal_config

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
    """GET /api/meta + the X-RP-Client-Version floor check (cloud plan Phase 9)."""

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
        # Auth ON = control mode, where the floor is enforced.
        self.client = TestClient(
            create_fastapi_app(self.app, auth=self.auth),
            raise_server_exceptions=False,
        )
        # Local mode (no auth) — floor never enforced.
        self.local_client = TestClient(
            create_fastapi_app(self.app), raise_server_exceptions=False
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _auth(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(extra or {})
        return headers

    def test_meta_returns_server_version_and_floors(self) -> None:
        from backend.version import (
            MIN_DAEMON_VERSION,
            MIN_PROXY_VERSION,
            SERVER_VERSION,
        )

        resp = self.client.get("/api/meta")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["server_version"], SERVER_VERSION)
        self.assertEqual(body["min_daemon_version"], MIN_DAEMON_VERSION)
        self.assertEqual(body["min_proxy_version"], MIN_PROXY_VERSION)

    def test_meta_reports_mode_capabilities(self) -> None:
        control = self.client.get("/api/meta")
        self.assertEqual(control.status_code, 200, control.text)
        self.assertEqual(control.json()["mode"], "control")
        self.assertTrue(control.json()["capabilities"]["hosted_control"])
        self.assertFalse(control.json()["capabilities"]["local_data_plane_http"])
        self.assertFalse(control.json()["capabilities"]["resource_registration"])
        self.assertFalse(control.json()["capabilities"]["resource_association"])
        self.assertFalse(control.json()["capabilities"]["sandbox_sync"])

        local = self.local_client.get("/api/meta")
        self.assertEqual(local.status_code, 200, local.text)
        self.assertEqual(local.json()["mode"], "local")
        self.assertFalse(local.json()["capabilities"]["hosted_control"])
        self.assertTrue(local.json()["capabilities"]["local_data_plane_http"])

    def test_meta_is_unauthenticated(self) -> None:
        # A client must be able to discover the floor before holding a token.
        resp = self.client.get("/api/meta")
        self.assertEqual(resp.status_code, 200)

    def test_in_range_client_passes(self) -> None:
        from backend.version import SERVER_VERSION

        resp = self.client.get(
            "/api/projects",
            headers=self._auth({"X-RP-Client-Version": SERVER_VERSION}),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_below_floor_client_rejected_with_upgrade_message(self) -> None:
        resp = self.client.get(
            "/api/projects",
            headers=self._auth({"X-RP-Client-Version": "0.0001"}),
        )
        self.assertEqual(resp.status_code, 426, resp.text)
        body = resp.json()
        self.assertEqual(body["error_code"], "client_too_old")
        self.assertIn("upgrade", body["detail"])
        # The floor is named so the client knows the target.
        self.assertIn("min_version", body)

    def test_missing_version_header_is_tolerated(self) -> None:
        # No header ⇒ pre-Phase-9 client; served (auth still applies normally).
        resp = self.client.get("/api/projects", headers=self._auth())
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_local_mode_never_enforces_floor(self) -> None:
        # An ancient client against local mode is served unchanged.
        resp = self.local_client.get(
            "/api/projects", headers={"X-RP-Client-Version": "0.0001"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_below_floor_rejected_before_auth(self) -> None:
        # The 426 fires even without a valid token (it precedes the auth check),
        # so an outdated client gets "upgrade", not a confusing 401.
        resp = self.client.get(
            "/api/projects", headers={"X-RP-Client-Version": "0.0001"}
        )
        self.assertEqual(resp.status_code, 426)

    def test_proxy_header_literal_matches_backend_constant(self) -> None:
        # The stdlib-only proxy duplicates the header name as a literal (it can't
        # import backend.version). Pin the two together so a rename can't desync
        # the daemon/proxy from the control-plane check.
        from pathlib import Path as _Path

        from backend.version import CLIENT_VERSION_HEADER

        proxy_src = (
            _Path(__file__).resolve().parents[2] / "mcp_server" / "proxy.py"
        ).read_text(encoding="utf-8")
        self.assertIn(f'"{CLIENT_VERSION_HEADER}"', proxy_src)


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

    def test_daemon_loopback_exposes_data_plane_mcp_surface(self) -> None:
        from backend.daemon_loopback import create_daemon_loopback_app

        class _StubLinks:
            @staticmethod
            def project_for_repo(*, repo_root: str) -> str:
                return "proj_1"

            @staticmethod
            def link(*, repo_root: str, project_id: str) -> None:
                return None

        class _StubDaemon:
            loopback_secret = "secret"
            project_links = _StubLinks()

            class control:
                @staticmethod
                def list_tools():
                    return []

        client = TestClient(
            create_daemon_loopback_app(daemon=_StubDaemon()),
            raise_server_exceptions=False,
        )
        headers = {"Authorization": "Bearer secret"}
        tools = client.get("/mcp/tools", headers=headers)
        self.assertEqual(tools.status_code, 200, tools.text)
        names = {tool["name"] for tool in tools.json()["tools"]}
        self.assertNotIn("resource.register_file", names)
        self.assertNotIn("sandbox.get", names)
        self.assertNotIn("claim.create", names)

        called = client.post(
            "/mcp/call",
            json={"name": "resource.register_file", "arguments": {"path": "x.txt"}},
            headers=headers,
        )
        self.assertEqual(called.status_code, 400, called.text)
        self.assertEqual(called.json()["error_code"], "data_plane_forwarding_unavailable")

    def test_local_app_builder_is_a_plain_research_plugin_app(self) -> None:
        from backend.composition import build_local_app

        app = build_local_app(
            repo_root=self.repo, db_path=self.repo / ".research_plugin" / "state.sqlite"
        )
        self.addCleanup(app.shutdown)
        self.assertIsInstance(app, ResearchPluginApp)


if __name__ == "__main__":
    unittest.main()
