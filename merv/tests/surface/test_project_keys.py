"""Project-scoped API-key lifecycle and the Phase-A authorization boundaries.

De-profiled: keys carry no local/cloud profile. The minted record exposes no
``profile`` attribute and ``create()`` rejects a ``profile`` kwarg.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
import jwt
from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.surface.auth import SupabaseVerifier, UnauthorizedError
from merv.brain.surface.project_key_store import SqlProjectKeyRepository
from merv.brain.surface.project_keys import ProjectKeyRecord, ProjectKeys
from merv.brain.surface.transport.http_api import create_fastapi_app
from merv.brain.surface.transport.http_policy import HttpSurfacePolicy

SECRET = "project-key-tests-jwt-secret-32-bytes"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
RR_KEY = "rr_sk_regression"
RR_DIGEST = hashlib.sha256(RR_KEY.encode()).hexdigest()


def _token(user_id: str) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "session_id": f"session-{user_id[:4]}",
        },
        SECRET,
        algorithm="HS256",
    )


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _postgrest(request: httpx.Request) -> httpx.Response:
    if f"eq.{RR_DIGEST}" in str(request.url):
        return httpx.Response(200, json=[{"user_id": USER_B}])
    return httpx.Response(200, json=[])


class ProjectKeySurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.root,
            db_path=self.root / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.keys = ProjectKeys(repository=SqlProjectKeyRepository(store=self.app.store))
        self.verifier = SupabaseVerifier(
            supabase_url="https://example.supabase.co",
            jwt_secret=SECRET,
            service_key="service-key",
            project_keys=self.keys,
        )
        self.verifier._http = httpx.Client(transport=httpx.MockTransport(_postgrest))
        self.client = TestClient(
            create_fastapi_app(
                self.app.http,
                surface_policy=HttpSurfacePolicy.for_surface(
                    restrict_cors=True, hosted_control=True
                ),
                auth=self.verifier,
            ),
            raise_server_exceptions=False,
        )
        self.jwt_a = _token(USER_A)
        self.jwt_b = _token(USER_B)
        self.project_a = self._create_project("Key Project A", self.jwt_a)
        self.project_b = self._create_project("Key Project B", self.jwt_a)
        minted = self._mint(
            project_id=self.project_a,
            sandbox_seconds_ceiling=3600,
            blob_bytes_ceiling=8,
        )
        self.key = minted["secret"]
        self.key_id = minted["key"]["id"]

    def tearDown(self) -> None:
        self.verifier._http.close()
        self.app.shutdown()
        self.tmp.cleanup()

    def _create_project(self, name: str, credential: str) -> str:
        response = self.client.post(
            "/api/projects", json={"name": name}, headers=_bearer(credential)
        )
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["id"])

    def _mint(self, *, project_id: str, **fields: object) -> dict:
        response = self.client.post(
            f"/api/projects/{project_id}/keys",
            json=dict(fields),
            headers=_bearer(self.jwt_a),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _add_member(self, project_id: str, user_id: str) -> None:
        added = self.client.post(
            f"/api/projects/{project_id}/members",
            json={"user_id": user_id},
            headers=_bearer(self.jwt_a),
        )
        self.assertEqual(added.status_code, 201, added.text)

    def test_mint_verify_lineage_expiry_and_owner_only_listing(self) -> None:
        self.assertTrue(self.key.startswith("mk_"))
        principal = self.verifier.verify_bearer(f"Bearer {self.key}")
        self.assertEqual(principal.key_id, self.key_id)
        self.assertEqual(principal.key_project_id, self.project_a)
        self.assertEqual(principal.key_sandbox_seconds_ceiling, 3600)
        self.assertEqual(principal.key_blob_bytes_ceiling, 8)

        with self.app.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_api_keys WHERE id = ?", (self.key_id,)
            ).fetchone()
        self.assertEqual(
            row["secret_digest"], hashlib.sha256(self.key.encode()).hexdigest()
        )
        self.assertNotEqual(row["secret_digest"], self.key)
        self.assertEqual(row["tenant_id"], "local")
        self.assertIsNone(row["audience"])  # no MERV_OAUTH_RESOURCE_URI configured

        child = self._mint(project_id=self.project_a, parent_key_id=self.key_id)
        self.assertEqual(child["key"]["parent_key_id"], self.key_id)
        listed = self.client.get(
            f"/api/projects/{self.project_a}/keys", headers=_bearer(self.jwt_a)
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            {item["id"] for item in listed.json()["keys"]},
            {self.key_id, child["key"]["id"]},
        )
        self.assertNotIn("secret", json.dumps(listed.json()))
        self.assertNotIn("secret_digest", json.dumps(listed.json()))

        self._add_member(self.project_a, USER_B)
        other_owner = self.client.get(
            f"/api/projects/{self.project_a}/keys", headers=_bearer(self.jwt_b)
        )
        self.assertEqual(other_owner.json(), {"keys": []})
        nonowner_revoke = self.client.post(
            f"/api/projects/{self.project_a}/keys/{self.key_id}/revoke",
            headers=_bearer(self.jwt_b),
        )
        self.assertEqual(nonowner_revoke.status_code, 404, nonowner_revoke.text)

        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE project_api_keys SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", child["key"]["id"]),
            )
        with self.assertRaises(UnauthorizedError):
            self.verifier.verify_bearer(f"Bearer {child['secret']}")

    def test_minted_record_has_no_profile_and_create_rejects_profile_kwarg(self) -> None:
        result = self.keys.create(project_id=self.project_a, owner_user_id=USER_A)
        self.assertNotIn("profile", result["key"])
        record = self.keys._repository.by_id(key_id=str(result["key"]["id"]))
        self.assertIsInstance(record, ProjectKeyRecord)
        self.assertFalse(hasattr(record, "profile"))
        with self.assertRaises(TypeError):
            self.keys.create(
                project_id=self.project_a, owner_user_id=USER_A, profile="cloud"
            )
        # The REST create rejects any unknown field rather than 201-ing and
        # silently dropping it (FIX 7).
        over_http = self.client.post(
            f"/api/projects/{self.project_a}/keys",
            json={"profile": "cloud"},
            headers=_bearer(self.jwt_a),
        )
        self.assertEqual(over_http.status_code, 400, over_http.text)
        self.assertEqual(over_http.json()["fields"], ["profile"])

    def test_revocation_is_immediate_after_a_successful_lookup(self) -> None:
        self.assertEqual(
            self.verifier.verify_bearer(f"Bearer {self.key}").key_id, self.key_id
        )
        revoked = self.client.post(
            f"/api/projects/{self.project_a}/keys/{self.key_id}/revoke",
            headers=_bearer(self.jwt_a),
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertTrue(revoked.json()["key"]["revoked_at"])
        with self.assertRaises(UnauthorizedError):
            self.verifier.verify_bearer(f"Bearer {self.key}")

    def test_key_management_requires_a_supabase_session(self) -> None:
        self._add_member(self.project_a, USER_B)
        for credential in (self.key, RR_KEY):
            requests = (
                self.client.post(
                    f"/api/projects/{self.project_a}/keys",
                    json={},
                    headers=_bearer(credential),
                ),
                self.client.get(
                    f"/api/projects/{self.project_a}/keys",
                    headers=_bearer(credential),
                ),
                self.client.post(
                    f"/api/projects/{self.project_a}/keys/{self.key_id}/revoke",
                    headers=_bearer(credential),
                ),
            )
            for response in requests:
                with self.subTest(credential=credential[:6]):
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(
                        response.json()["error_code"], "human_session_required"
                    )
        # The key still authenticates for ordinary use.
        self.assertEqual(
            self.verifier.verify_bearer(f"Bearer {self.key}").key_id, self.key_id
        )

    def test_exact_scope_precedes_membership_on_rest_and_mcp(self) -> None:
        same = self.client.get(
            f"/api/projects/{self.project_a}", headers=_bearer(self.key)
        )
        self.assertEqual(same.status_code, 200, same.text)
        cross = self.client.get(
            f"/api/projects/{self.project_b}", headers=_bearer(self.key)
        )
        self.assertEqual(cross.status_code, 403, cross.text)
        self.assertEqual(cross.json()["error_code"], "project_scope_forbidden")

        legacy = self.client.post(
            "/mcp/call",
            json={
                "name": "workflow.status_and_next",
                "arguments": {"project_id": self.project_b},
            },
            headers=_bearer(self.key),
        )
        self.assertEqual(legacy.status_code, 403, legacy.text)
        self.assertEqual(legacy.json()["error_code"], "project_scope_forbidden")

        streamable = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "workflow.status_and_next",
                    "arguments": {"project_id": self.project_b},
                },
            },
            headers={**_bearer(self.key), "Accept": "application/json"},
        )
        self.assertEqual(streamable.status_code, 403, streamable.text)
        self.assertEqual(
            streamable.json()["error"]["data"]["error_code"],
            "project_scope_forbidden",
        )

        admin = self.client.post("/api/admin/cleanup", headers=_bearer(self.key))
        self.assertEqual(admin.status_code, 403, admin.text)
        self.assertEqual(admin.json()["error_code"], "project_scope_forbidden")

    def test_internal_tool_forbidden_over_mcp_for_key(self) -> None:
        # Same project, but claim.list is internal → refused over both transports.
        legacy = self.client.post(
            "/mcp/call",
            json={"name": "claim.list", "arguments": {"project_id": self.project_a}},
            headers=_bearer(self.key),
        )
        self.assertEqual(legacy.status_code, 403, legacy.text)
        self.assertEqual(legacy.json()["error_code"], "tool_visibility_forbidden")
        streamable = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "claim.list",
                    "arguments": {"project_id": self.project_a},
                },
            },
            headers={**_bearer(self.key), "Accept": "application/json"},
        )
        self.assertEqual(streamable.status_code, 403, streamable.text)
        self.assertEqual(
            streamable.json()["error"]["data"]["error_code"],
            "tool_visibility_forbidden",
        )

    def test_project_key_cannot_access_operator_diagnostics(self) -> None:
        for path in (
            f"/api/activity?project_id={self.project_a}",
            f"/api/debug/tool-calls?project_id={self.project_a}",
        ):
            response = self.client.get(path, headers=_bearer(self.key))
            with self.subTest(path=path):
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    response.json()["error_code"], "project_scope_forbidden"
                )
        # A JWT operator on the same paths is unaffected.
        self.assertEqual(
            self.client.get(
                f"/api/activity?project_id={self.project_a}", headers=_bearer(self.jwt_a)
            ).status_code,
            200,
        )

    def test_key_cannot_submit_foreign_project_review_session(self) -> None:
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_requests (
                  id, project_id, target_type, target_id, role, capability_hash,
                  status, target_snapshot_id, expires_at, created_at
                ) VALUES (?, ?, 'experiment', 'exp_foreign', 'design_reviewer',
                          ?, 'started', 'foreign-snapshot', ?, ?)
                """,
                (
                    "rreq_foreign",
                    self.project_b,
                    "a" * 64,
                    "2099-01-01T00:00:00Z",
                    "2026-07-22T00:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO review_sessions (
                  id, request_id, caller_session_id, independence, status, created_at
                ) VALUES (?, ?, 'foreign-reviewer', 'verified_agent_review',
                          'started', ?)
                """,
                ("rvs_foreign", "rreq_foreign", "2026-07-22T00:00:00Z"),
            )
        response = self.client.post(
            "/mcp/call",
            json={
                "name": "review.submit",
                "arguments": {
                    "review_session_id": "rvs_foreign",
                    "verdict": "pass",
                    "synopsis": (
                        "The foreign design was reviewed, and its evidence supports "
                        "the stated decision."
                    ),
                },
            },
            headers=_bearer(self.key),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error_code"], "project_scope_forbidden")

    def test_jwt_and_rr_principals_carry_no_key_context(self) -> None:
        jwt_principal = self.verifier.verify_bearer(f"Bearer {self.jwt_a}")
        rr_principal = self.verifier.verify_bearer(f"Bearer {RR_KEY}")
        for principal in (jwt_principal, rr_principal):
            self.assertIsNone(principal.key_id)
            self.assertIsNone(principal.key_project_id)
            self.assertFalse(hasattr(principal, "profile"))
        self.assertTrue(jwt_principal.client_id.startswith("jwt:"))
        self.assertEqual(rr_principal.user_id, USER_B)
        self.assertTrue(rr_principal.client_id.startswith("key:"))

    def test_mlflow_gate_rejects_project_key_but_allows_other_audiences(self) -> None:
        denied = self.client.get("/internal/auth/mlflow", headers=_bearer(self.key))
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error_code"], "credential_audience_forbidden")
        self.assertEqual(
            self.client.get(
                "/internal/auth/mlflow", headers=_bearer(self.jwt_a)
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.get(
                "/internal/auth/mlflow", headers=_bearer(RR_KEY)
            ).status_code,
            204,
        )


if __name__ == "__main__":
    unittest.main()
