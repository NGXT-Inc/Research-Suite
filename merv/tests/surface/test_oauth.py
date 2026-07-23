"""OAuth DCR, PKCE consent, token rotation, and MCP integration.

De-profiled port: OAuth access bearers are project (mk_) keys with an immutable
audience binding and NO local/cloud profile. The idempotency-store replay test
from the source branch is intentionally absent — the surface idempotency store
was cut by owner ruling and no tool carries an idempotency key.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import jwt
from starlette.requests import Request
from fastapi.testclient import TestClient

from merv.brain.kernel.utils import parse_iso
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.surface.auth import SupabaseVerifier
from merv.brain.surface.oauth import OAuthError, OAuthService
from merv.brain.surface.oauth_store import SqlOAuthRepository
from merv.brain.surface.project_key_store import SqlProjectKeyRepository
from merv.brain.surface.project_keys import ProjectKeys
from merv.brain.surface.transport.http_api import create_fastapi_app
from merv.brain.surface.transport.http_policy import HttpSurfacePolicy
from tests.support.brain import TestBrain

SECRET = "oauth-tests-jwt-secret-at-least-32-bytes"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ISSUER = "https://merv.example"
RESOURCE = f"{ISSUER}/mcp"
REDIRECT_URI = "https://client.example/oauth/callback"
VERIFIER = "a" * 43


def _jwt(user_id: str) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "session_id": f"oauth-{user_id[:4]}",
        },
        SECRET,
        algorithm="HS256",
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=root,
            db_path=root / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.keys = ProjectKeys(
            repository=SqlProjectKeyRepository(store=self.app.store)
        )
        self.oauth = OAuthService(
            repository=SqlOAuthRepository(store=self.app.store),
            project_keys=self.keys,
            is_project_member=self.app.projects.is_member,
        )
        self.verifier = SupabaseVerifier(
            supabase_url="https://example.supabase.co",
            jwt_secret=SECRET,
            project_keys=self.keys,
        )
        self.client = TestClient(
            create_fastapi_app(
                self.app.http,
                surface_policy=HttpSurfacePolicy.for_surface(
                    restrict_cors=True, hosted_control=True
                ),
                auth=self.verifier,
                oauth_service=self.oauth,
                oauth_resource_uri=RESOURCE,
                allowed_origins=["https://ui.example"],
                ui_base_url="https://ui.example/merv",
            ),
            base_url=ISSUER,
            raise_server_exceptions=False,
        )
        self.jwt_a = _jwt(USER_A)
        self.jwt_b = _jwt(USER_B)
        self.project_a = self._create_project("OAuth Project A", self.jwt_a)

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _create_project(self, name: str, token: str) -> str:
        response = self.client.post(
            "/api/projects", json={"name": name}, headers=_bearer(token)
        )
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["id"])

    def _register(
        self,
        *,
        redirect_uris: list[str] | None = None,
        grants: list[str] | None = None,
        **metadata,
    ) -> dict:
        response = self.client.post(
            "/oauth/register",
            json={
                "client_name": "Replit Agent",
                "redirect_uris": redirect_uris or [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
                "grant_types": grants or ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                **metadata,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _authorization_params(
        self,
        client_id: str,
        *,
        redirect_uri: str = REDIRECT_URI,
        verifier: str = VERIFIER,
        state: str = "client-state",
    ) -> dict[str, str]:
        return {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        }

    def _authorize(
        self,
        client_id: str,
        *,
        project_id: str | None = None,
        token: str | None = None,
        params: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, list[str]]]:
        params = params or self._authorization_params(client_id)
        response = self.client.post(
            "/oauth/authorize",
            json={
                **params,
                "decision": "approve",
                "project_id": project_id or self.project_a,
            },
            headers=_bearer(token or self.jwt_a),
        )
        self.assertEqual(response.status_code, 200, response.text)
        redirect = response.json()["redirect_to"]
        return redirect, parse_qs(urlsplit(redirect).query)

    def _exchange(
        self,
        *,
        client_id: str,
        code: str,
        verifier: str = VERIFIER,
        redirect_uri: str = REDIRECT_URI,
    ):
        return self.client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "resource": RESOURCE,
            },
        )

    def _mint_oauth_tokens(self) -> tuple[dict, dict]:
        registration = self._register()
        _redirect, query = self._authorize(registration["client_id"])
        response = self._exchange(
            client_id=registration["client_id"], code=query["code"][0]
        )
        self.assertEqual(response.status_code, 200, response.text)
        return registration, response.json()

    def test_discovery_metadata_and_mcp_challenge_are_exact(self) -> None:
        metadata = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertEqual(
            metadata.json(),
            {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                "token_endpoint": f"{ISSUER}/oauth/token",
                "registration_endpoint": f"{ISSUER}/oauth/register",
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
                "protected_resources": [RESOURCE],
            },
        )
        protected = self.client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(
            protected.json(),
            {
                "resource": RESOURCE,
                "authorization_servers": [ISSUER],
                "bearer_methods_supported": ["header"],
            },
        )
        unauthorized = self.client.post("/mcp", json={})
        self.assertEqual(unauthorized.status_code, 401, unauthorized.text)
        self.assertEqual(
            unauthorized.headers["www-authenticate"],
            f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"',
        )

    def test_dcr_accepts_only_public_strict_redirect_clients(self) -> None:
        registration = self._register(
            redirect_uris=[REDIRECT_URI, "http://localhost:43110/callback"]
        )
        self.assertTrue(registration["client_id"].startswith("oauthc_"))
        self.assertNotIn("client_secret", registration)
        self.assertEqual(registration["token_endpoint_auth_method"], "none")
        with self.app.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_clients WHERE client_id = ?",
                (registration["client_id"],),
            ).fetchone()
        self.assertEqual(row["client_name"], "Replit Agent")

        rejected = (
            {"redirect_uris": ["http://attacker.example/callback"]},
            {"redirect_uris": ["https://client.example/cb#fragment"]},
            {"redirect_uris": ["https://client.example\\@attacker.example/cb"]},
            {"redirect_uris": ["http://127.0.0.1/callback"]},
            {"token_endpoint_auth_method": "client_secret_basic"},
            {"grant_types": ["implicit"]},
            {"response_types": ["token"]},
            {"scope": "mcp"},
        )
        for override in rejected:
            payload = {
                "client_name": "Rejected client",
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                **override,
            }
            with self.subTest(override=override):
                response = self.client.post("/oauth/register", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(
                    response.json()["error"],
                    {"invalid_redirect_uri", "invalid_client_metadata"},
                )
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_code_pkce_exchange_mints_working_key_for_mcp_tools_list(
        self,
    ) -> None:
        registration = self._register()
        params = self._authorization_params(registration["client_id"])
        begun = self.client.get(
            "/oauth/authorize", params=params, follow_redirects=False
        )
        self.assertEqual(begun.status_code, 302, begun.text)
        self.assertTrue(
            begun.headers["location"].startswith(
                "https://ui.example/merv/oauth/authorize?"
            )
        )
        details = self.client.get(
            "/oauth/authorize/details", params=params, headers=_bearer(self.jwt_a)
        )
        self.assertEqual(
            details.json(),
            {
                "client_id": registration["client_id"],
                "client_name": "Replit Agent",
                "resource": RESOURCE,
            },
        )
        redirect, query = self._authorize(registration["client_id"], params=params)
        self.assertTrue(redirect.startswith(f"{REDIRECT_URI}?"))
        self.assertEqual(query["state"], ["client-state"])
        self.assertEqual(query["iss"], [ISSUER])
        code = query["code"][0]

        exchanged = self._exchange(client_id=registration["client_id"], code=code)
        self.assertEqual(exchanged.status_code, 200, exchanged.text)
        tokens = exchanged.json()
        self.assertTrue(tokens["access_token"].startswith("mk_"))
        self.assertTrue(tokens["refresh_token"].startswith("mrt_"))
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(exchanged.headers["cache-control"], "no-store")
        with self.app.store.connect() as conn:
            stored_code = conn.execute(
                "SELECT * FROM oauth_authorization_codes WHERE code_digest = ?",
                (hashlib.sha256(code.encode()).hexdigest(),),
            ).fetchone()
            key = conn.execute(
                "SELECT * FROM project_api_keys WHERE secret_digest = ?",
                (hashlib.sha256(tokens["access_token"].encode()).hexdigest(),),
            ).fetchone()
            refresh = conn.execute("SELECT * FROM oauth_refresh_tokens").fetchone()
        self.assertNotEqual(stored_code["code_digest"], code)
        code_created = parse_iso(stored_code["created_at"])
        code_expires = parse_iso(stored_code["expires_at"])
        self.assertIsNotNone(code_created)
        self.assertIsNotNone(code_expires)
        assert code_created is not None and code_expires is not None
        self.assertLessEqual((code_expires - code_created).total_seconds(), 60)
        # De-profiled: the minted access bearer carries project + audience +
        # oauth family, and there is no profile column at all.
        self.assertNotIn("profile", key.keys())
        self.assertEqual(key["project_id"], self.project_a)
        self.assertEqual(key["audience"], RESOURCE)
        self.assertEqual(key["oauth_family_id"], refresh["family_id"])
        self.assertIsNone(key["sandbox_seconds_ceiling"])
        self.assertIsNone(key["blob_bytes_ceiling"])
        self.assertNotEqual(refresh["secret_digest"], tokens["refresh_token"])

        forbidden_rest = self.client.get(
            f"/api/projects/{self.project_a}",
            headers=_bearer(tokens["access_token"]),
        )
        self.assertEqual(forbidden_rest.status_code, 403, forbidden_rest.text)
        self.assertEqual(
            forbidden_rest.json()["error_code"], "credential_audience_forbidden"
        )
        legacy_mcp = self.client.get(
            "/mcp/tools", headers=_bearer(tokens["access_token"])
        )
        self.assertEqual(legacy_mcp.status_code, 200, legacy_mcp.text)

        encoded = base64.b64encode(
            f"merv:{tokens['access_token']}".encode()
        ).decode()
        mlflow_gate = self.client.get(
            "/internal/auth/mlflow", headers={"Authorization": f"Basic {encoded}"}
        )
        self.assertEqual(mlflow_gate.status_code, 403, mlflow_gate.text)
        self.assertEqual(
            mlflow_gate.json()["error_code"], "credential_audience_forbidden"
        )

        initialized = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "oauth-test", "version": "1"},
                },
            },
            headers=_bearer(tokens["access_token"]),
        )
        self.assertEqual(initialized.status_code, 200, initialized.text)
        session = initialized.headers["mcp-session-id"]
        headers = {**_bearer(tokens["access_token"]), "Mcp-Session-Id": session}
        ready = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        self.assertEqual(ready.status_code, 202, ready.text)
        listed = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertTrue(listed.json()["result"]["tools"])

        alias = TestClient(self.client.app, base_url="https://alias.example")
        try:
            wrong_origin = alias.get(
                "/mcp/tools", headers=_bearer(tokens["access_token"])
            )
        finally:
            alias.close()
        self.assertEqual(wrong_origin.status_code, 403, wrong_origin.text)
        self.assertEqual(
            wrong_origin.json()["error_code"], "credential_audience_forbidden"
        )

    def test_public_oauth_body_limits_stream_before_buffering(self) -> None:
        with (
            patch(
                "merv.brain.surface.transport.api.oauth._MAX_DCR_BODY_BYTES", 32
            ),
            patch(
                "merv.brain.surface.transport.api.oauth._MAX_TOKEN_BODY_BYTES", 32
            ),
            patch.object(
                Request,
                "body",
                side_effect=AssertionError("OAuth limits must not call request.body"),
            ),
        ):
            registration = self.client.post(
                "/oauth/register",
                content=b"x" * 33,
                headers={"Content-Type": "application/json"},
            )
            token = self.client.post(
                "/oauth/token",
                content=b"x" * 33,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        self.assertEqual(registration.status_code, 400, registration.text)
        self.assertEqual(registration.json()["error"], "invalid_client_metadata")
        self.assertIn("too large", registration.json()["error_description"])
        self.assertEqual(token.status_code, 400, token.text)
        self.assertEqual(token.json()["error"], "invalid_request")
        self.assertIn("too large", token.json()["error_description"])

    def test_codes_are_single_use_expiring_and_pkce_bound(self) -> None:
        registration = self._register(grants=["authorization_code"])
        _redirect, query = self._authorize(registration["client_id"])
        code = query["code"][0]
        wrong = self._exchange(
            client_id=registration["client_id"], code=code, verifier="b" * 43
        )
        self.assertEqual(wrong.status_code, 400, wrong.text)
        self.assertEqual(wrong.json()["error"], "invalid_grant")
        right = self._exchange(client_id=registration["client_id"], code=code)
        self.assertEqual(right.status_code, 200, right.text)
        self.assertNotIn("refresh_token", right.json())
        replay = self._exchange(client_id=registration["client_id"], code=code)
        self.assertEqual(replay.json()["error"], "invalid_grant")

        _redirect, expiring_query = self._authorize(registration["client_id"])
        expiring = expiring_query["code"][0]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE oauth_authorization_codes SET expires_at = ? WHERE code_digest = ?",
                ("2000-01-01T00:00:00Z", hashlib.sha256(expiring.encode()).hexdigest()),
            )
        expired = self._exchange(client_id=registration["client_id"], code=expiring)
        self.assertEqual(expired.json()["error"], "invalid_grant")

    def test_refresh_rotation_revokes_predecessor_and_replay_fails(self) -> None:
        registration, first = self._mint_oauth_tokens()
        refreshed = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": registration["client_id"],
                "refresh_token": first["refresh_token"],
                "resource": RESOURCE,
            },
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        second = refreshed.json()
        self.assertNotEqual(second["access_token"], first["access_token"])
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])

        old_access = self.client.post(
            "/mcp", json={}, headers=_bearer(first["access_token"])
        )
        self.assertEqual(old_access.status_code, 401, old_access.text)
        new_access = self.client.post(
            "/mcp", json={}, headers=_bearer(second["access_token"])
        )
        self.assertNotEqual(new_access.status_code, 401, new_access.text)
        replay = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": registration["client_id"],
                "refresh_token": first["refresh_token"],
                "resource": RESOURCE,
            },
        )
        self.assertEqual(replay.json()["error"], "invalid_grant")
        replay_revoked_access = self.client.post(
            "/mcp", json={}, headers=_bearer(second["access_token"])
        )
        self.assertEqual(replay_revoked_access.status_code, 401)
        replay_revoked_refresh = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": registration["client_id"],
                "refresh_token": second["refresh_token"],
                "resource": RESOURCE,
            },
        )
        self.assertEqual(replay_revoked_refresh.json()["error"], "invalid_grant")

        with self.app.store.connect() as conn:
            keys = conn.execute(
                "SELECT * FROM project_api_keys ORDER BY created_at, id"
            ).fetchall()
            refreshes = conn.execute(
                "SELECT * FROM oauth_refresh_tokens ORDER BY created_at, id"
            ).fetchall()
        self.assertEqual(len(keys), 2)
        predecessor = next(key for key in keys if key["parent_key_id"] is None)
        successor = next(key for key in keys if key["parent_key_id"] is not None)
        self.assertTrue(predecessor["revoked_at"])
        self.assertEqual(successor["parent_key_id"], predecessor["id"])
        first_refresh = next(
            token for token in refreshes if token["parent_token_id"] is None
        )
        second_refresh = next(
            token for token in refreshes if token["parent_token_id"] is not None
        )
        self.assertTrue(first_refresh["consumed_at"])
        self.assertEqual(second_refresh["parent_token_id"], first_refresh["id"])
        self.assertEqual(second_refresh["family_id"], first_refresh["family_id"])
        self.assertTrue(second_refresh["revoked_at"])

        next_registration, next_tokens = self._mint_oauth_tokens()
        next_key = self.keys.verify_secret(secret=next_tokens["access_token"])
        self.assertIsNotNone(next_key)
        assert next_key is not None
        self.keys.revoke(project_id=self.project_a, key_id=next_key.id, owner_user_id=USER_A)
        revoked_refresh = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": next_registration["client_id"],
                "refresh_token": next_tokens["refresh_token"],
                "resource": RESOURCE,
            },
        )
        self.assertEqual(revoked_refresh.json()["error"], "invalid_grant")

    def test_oauth_access_keys_of_one_grant_share_the_oauth_family(self) -> None:
        """Rotation keeps a single stable oauth_family_id across access keys —
        the grant-scoped identity that later phases key on (the branch's
        idempotency replay used it; the store itself is cut by ruling)."""
        registration, first = self._mint_oauth_tokens()
        refreshed = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": registration["client_id"],
                "refresh_token": first["refresh_token"],
                "resource": RESOURCE,
            },
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        with self.app.store.connect() as conn:
            families = {
                row["oauth_family_id"]
                for row in conn.execute(
                    "SELECT oauth_family_id FROM project_api_keys ORDER BY created_at, id"
                ).fetchall()
            }
        self.assertEqual(len(families), 1)
        self.assertNotIn(None, families)

    def test_consent_requires_supabase_session_membership_and_never_open_redirects(
        self,
    ) -> None:
        registration = self._register()
        params = self._authorization_params(registration["client_id"])
        no_session = self.client.get("/oauth/authorize/details", params=params)
        self.assertEqual(no_session.status_code, 401, no_session.text)

        _registration, oauth_tokens = self._mint_oauth_tokens()
        project_key_session = self.client.get(
            "/oauth/authorize/details",
            params=params,
            headers=_bearer(oauth_tokens["access_token"]),
        )
        self.assertEqual(project_key_session.status_code, 403, project_key_session.text)
        self.assertEqual(
            project_key_session.json()["error_code"],
            "credential_audience_forbidden",
        )

        project_b = self._create_project("OAuth Project B", self.jwt_b)
        denied_redirect, denied = self._authorize(
            registration["client_id"], project_id=project_b
        )
        self.assertTrue(denied_redirect.startswith(REDIRECT_URI))
        self.assertEqual(denied["error"], ["access_denied"])
        self.assertEqual(denied["state"], ["client-state"])
        self.assertEqual(denied["iss"], [ISSUER])

        for client_id, redirect_uri in (
            (registration["client_id"], "https://attacker.example/callback"),
            ("unknown-client", "https://attacker.example/callback"),
        ):
            with self.subTest(client_id=client_id):
                attack = self.client.get(
                    "/oauth/authorize",
                    params=self._authorization_params(
                        client_id, redirect_uri=redirect_uri
                    ),
                    follow_redirects=False,
                )
                self.assertEqual(attack.status_code, 400, attack.text)
                self.assertNotIn("location", attack.headers)

        invalid_pkce = self._authorization_params(registration["client_id"])
        invalid_pkce["code_challenge_method"] = "plain"
        safe_error = self.client.get(
            "/oauth/authorize", params=invalid_pkce, follow_redirects=False
        )
        self.assertEqual(safe_error.status_code, 302, safe_error.text)
        parsed = urlsplit(safe_error.headers["location"])
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}", REDIRECT_URI
        )
        error_query = parse_qs(parsed.query)
        self.assertEqual(error_query["error"], ["invalid_request"])
        self.assertEqual(error_query["state"], ["client-state"])
        self.assertEqual(error_query["iss"], [ISSUER])

    def test_token_endpoint_rejects_client_authentication_with_401(self) -> None:
        # RFC 6749 §5.2: a client that presented an Authorization header must
        # get 401 with a matching WWW-Authenticate challenge, not 400.
        registration, first = self._mint_oauth_tokens()
        response = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": registration["client_id"],
                "refresh_token": first["refresh_token"],
                "resource": RESOURCE,
            },
            headers={"Authorization": "Basic Zm9vOmJhcg=="},
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Basic")
        self.assertEqual(response.json()["error"], "invalid_client")

    def test_concurrent_refresh_replay_revokes_the_family(self) -> None:
        # A refresh token read as unconsumed but lost at the compare-and-set
        # (a concurrent exchange won the race) is reuse: the family must be
        # revoked, exactly as the sequential-replay path does.
        _registration, tokens = self._mint_oauth_tokens()

        class LosingRepository(SqlOAuthRepository):
            atomic_revocation_called = False

            def consume_refresh_token(self, *, token_id: str, consumed_at: str) -> bool:
                return False

            def revoke_refresh_family(self, *, family_id: str, revoked_at: str) -> None:
                raise AssertionError("split refresh-family revocation must not be used")

            def revoke_refresh_family_and_key_lineage(self, **kwargs: str) -> None:
                self.atomic_revocation_called = True
                super().revoke_refresh_family_and_key_lineage(**kwargs)

        repository = LosingRepository(store=self.app.store)
        racing = OAuthService(
            repository=repository,
            project_keys=self.keys,
            is_project_member=self.app.projects.is_member,
        )
        with self.assertRaises(OAuthError) as caught:
            racing.refresh(
                form={
                    "grant_type": "refresh_token",
                    "client_id": _registration["client_id"],
                    "refresh_token": tokens["refresh_token"],
                    "resource": RESOURCE,
                },
                canonical_resource=RESOURCE,
            )
        self.assertEqual(caught.exception.error, "invalid_grant")
        self.assertTrue(repository.atomic_revocation_called)
        with self.app.store.connect() as conn:
            refreshes = conn.execute(
                "SELECT revoked_at FROM oauth_refresh_tokens"
            ).fetchall()
        self.assertTrue(all(row["revoked_at"] for row in refreshes))
        stale = self.keys.verify_secret(secret=tokens["access_token"])
        self.assertIsNone(stale)

    def test_replay_revocation_rolls_back_refresh_and_key_together(self) -> None:
        _registration, tokens = self._mint_oauth_tokens()
        with self.app.store.connect() as conn:
            refresh = conn.execute(
                "SELECT family_id, current_key_id FROM oauth_refresh_tokens"
            ).fetchone()
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                CREATE TRIGGER reject_project_key_revocation
                BEFORE UPDATE OF revoked_at ON project_api_keys
                BEGIN
                  SELECT RAISE(ABORT, 'forced key revocation failure');
                END
                """
            )

        repository = SqlOAuthRepository(store=self.app.store)
        with self.assertRaises(sqlite3.IntegrityError):
            repository.revoke_refresh_family_and_key_lineage(
                family_id=refresh["family_id"],
                key_id=refresh["current_key_id"],
                project_id=self.project_a,
                owner_user_id=USER_A,
                revoked_at="2026-07-22T12:00:00Z",
            )

        with self.app.store.connect() as conn:
            refresh_revoked = conn.execute(
                "SELECT revoked_at FROM oauth_refresh_tokens"
            ).fetchone()["revoked_at"]
            key_revoked = conn.execute(
                "SELECT revoked_at FROM project_api_keys WHERE id = ?",
                (refresh["current_key_id"],),
            ).fetchone()["revoked_at"]
        self.assertIsNone(refresh_revoked)
        self.assertIsNone(key_revoked)
        self.assertIsNotNone(self.keys.verify_secret(secret=tokens["access_token"]))


if __name__ == "__main__":
    unittest.main()
