"""Identity: token mint/hash/resolve, expiry/revocation, LOCAL_PRINCIPAL.

Cloud plan Phase 7 (§3.2). The control plane resolves a bearer token to a
Principal; local mode runs auth off with the implicit LOCAL_PRINCIPAL. These
tests pin the token round trip, the constant-time hashed lookup, the
expired/revoked rejections, and resolve_auth_required's mode derivation.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.config import resolve_auth_required
from backend.services.identity import (
    LOCAL_PRINCIPAL,
    AuthError,
    AuthService,
    Principal,
    hash_token,
)
from backend.state.store import StateStore


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AuthIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / ".research_plugin" / "state.sqlite"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(db_path=self.db)
        self.auth = AuthService(store=self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mint_resolve_round_trip(self) -> None:
        token = self.auth.mint_token(tenant_id="acme", label="ci")
        principal = self.auth.resolve(token=token)
        self.assertEqual(principal.tenant_id, "acme")
        self.assertEqual(principal.client_id, "control")

    def test_daemon_token_resolves_with_daemon_client_id(self) -> None:
        token = self.auth.mint_token(tenant_id="acme", client_id="daemon", label="daemon")
        principal = self.auth.resolve(token=token)
        self.assertEqual(principal.tenant_id, "acme")
        self.assertEqual(principal.client_id, "daemon")

    def test_plaintext_token_is_never_stored(self) -> None:
        token = self.auth.mint_token(tenant_id="acme")
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT token_hash FROM api_tokens"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        # Only the hash is at rest; the plaintext is not derivable from it.
        self.assertEqual(rows[0]["token_hash"], hash_token(token))
        self.assertNotIn(token, [r["token_hash"] for r in rows])

    def test_unknown_token_rejected(self) -> None:
        with self.assertRaises(AuthError):
            self.auth.resolve(token="rpt_not_a_real_token")

    def test_missing_token_rejected(self) -> None:
        with self.assertRaises(AuthError):
            self.auth.resolve(token=None)
        with self.assertRaises(AuthError):
            self.auth.resolve(token="")

    def test_expired_token_rejected(self) -> None:
        past = _iso(datetime.now(UTC) - timedelta(hours=1))
        token = self.auth.mint_token(tenant_id="acme", expires_at=past)
        with self.assertRaises(AuthError):
            self.auth.resolve(token=token)

    def test_future_expiry_still_valid(self) -> None:
        future = _iso(datetime.now(UTC) + timedelta(hours=1))
        token = self.auth.mint_token(tenant_id="acme", expires_at=future)
        self.assertEqual(self.auth.resolve(token=token).tenant_id, "acme")

    def test_revoked_token_rejected(self) -> None:
        token = self.auth.mint_token(tenant_id="acme")
        self.assertEqual(self.auth.resolve(token=token).tenant_id, "acme")
        self.auth.revoke_token(token=token)
        with self.assertRaises(AuthError):
            self.auth.resolve(token=token)

    def test_two_tenants_resolve_independently(self) -> None:
        a = self.auth.mint_token(tenant_id="tenant_a")
        b = self.auth.mint_token(tenant_id="tenant_b")
        self.assertEqual(self.auth.resolve(token=a).tenant_id, "tenant_a")
        self.assertEqual(self.auth.resolve(token=b).tenant_id, "tenant_b")

    def test_minting_a_token_creates_the_tenant(self) -> None:
        self.auth.mint_token(tenant_id="acme")
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT id FROM tenants WHERE id = ?", ("acme",)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)

    def test_local_principal_shape(self) -> None:
        self.assertEqual(LOCAL_PRINCIPAL, Principal(tenant_id="local", client_id="local"))

    def test_resolve_auth_required_by_mode(self) -> None:
        # Local (default) ⇒ auth off; control ⇒ auth on; daemon ⇒ off (it
        # authenticates upstream, not its own callers).
        self.assertFalse(resolve_auth_required(env={}))
        self.assertFalse(resolve_auth_required(env={"RESEARCH_PLUGIN_MODE": "local"}))
        self.assertTrue(resolve_auth_required(env={"RESEARCH_PLUGIN_MODE": "control"}))
        self.assertFalse(resolve_auth_required(env={"RESEARCH_PLUGIN_MODE": "daemon"}))


if __name__ == "__main__":
    unittest.main()
