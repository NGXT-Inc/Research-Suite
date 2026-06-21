"""Identity: principals, bearer tokens, and the auth resolution seam.

Cloud plan Phase 7 (§3.2). The control plane authenticates every request with
an ``Authorization: Bearer`` token that resolves to a ``Principal`` —
``{tenant_id, client_id}``. Local mode runs with auth OFF on loopback: there is
one implicit tenant ('local') and the principal is ``LOCAL_PRINCIPAL``, so this
whole module is dormant in local mode and load-bearing only in control mode.

Token hashing rationale (deliberate, documented): API tokens are HIGH-ENTROPY
bearer secrets minted with ``secrets.token_urlsafe`` — not user-chosen
passwords. A slow password hash (bcrypt/argon2) defends low-entropy secrets
against offline brute force; that threat does not apply here, because guessing a
256-bit random token is infeasible regardless of hash speed. A fast
``hashlib.sha256`` is the correct, standard choice for opaque API keys (it is
what GitHub/Stripe-style key stores do), and it keeps the lookup a single
indexed equality. We still compare with ``hmac.compare_digest`` so a resolved
row's stored hash is matched in constant time.

Never log or store the plaintext token. Mint returns it once; only the hash is
persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain.vocabulary import LOCAL_CLIENT_ID, LOCAL_TENANT_ID
from ..state.store import BaseStateStore
from ..utils import now_iso


@dataclass(frozen=True)
class Principal:
    """The authenticated identity behind a request.

    ``tenant_id`` scopes every project-level record; ``client_id`` identifies
    the calling machine/daemon within the tenant (lease holder identity, audit
    attribution). Local mode uses ``LOCAL_PRINCIPAL``.
    """

    tenant_id: str
    client_id: str


LOCAL_PRINCIPAL = Principal(tenant_id=LOCAL_TENANT_ID, client_id=LOCAL_CLIENT_ID)


def hash_token(token: str) -> str:
    """The stored form of a bearer token: its sha256 hex digest.

    See the module docstring for why a fast hash is correct here (high-entropy
    opaque secret, not a password).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthError(Exception):
    """Token is missing, invalid, expired, or revoked.

    Deliberately a thin local exception (not a ResearchPluginError): the HTTP
    layer maps it to a 401, which the domain error handler does not do.
    """


class AuthService:
    """Resolves bearer tokens to principals and mints/revokes them.

    Constructed only by the control composition (Phase 8). Local mode never
    builds one — ``create_fastapi_app(auth=None)`` keeps auth off — so this
    class is exercised by tests and the future control plane.
    """

    def __init__(self, *, store: BaseStateStore, client_id: str = "control") -> None:
        self.store = store
        # Default client id for newly minted tokens when the caller does not
        # specify one. Resolved principals read client_id from the token row so
        # daemon-only endpoints can distinguish daemon and hosted UI tokens.
        self.client_id = client_id

    # ---------- mint / revoke (out-of-band provisioning, plan Phase 7) ----------

    def ensure_tenant(self, *, tenant_id: str, name: str = "") -> str:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
                    (tenant_id, name, now_iso()),
                )
        return tenant_id

    def mint_token(
        self,
        *,
        tenant_id: str,
        client_id: str | None = None,
        label: str = "",
        expires_at: str | None = None,
    ) -> str:
        """Mint a token for a tenant and persist only its hash; return plaintext.

        The plaintext is returned exactly once — it is never recoverable from
        the store. Callers must hand it to the tenant out of band and discard it.
        """
        self.ensure_tenant(tenant_id=tenant_id)
        token = f"rpt_{secrets.token_urlsafe(32)}"
        resolved_client_id = (client_id or self.client_id).strip() or self.client_id
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO api_tokens (token_hash, tenant_id, client_id, label, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (hash_token(token), tenant_id, resolved_client_id, label, now_iso(), expires_at),
            )
        return token

    def revoke_token(self, *, token: str) -> bool:
        token_hash = hash_token(token)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE token_hash = ?",
                (now_iso(), token_hash),
            )
        return True

    # ---------- resolve (the middleware's call) ----------

    def resolve(self, *, token: str | None) -> Principal:
        """Resolve a bearer token to a Principal, or raise AuthError.

        Constant-time hash compare; rejects missing/unknown/expired/revoked.
        """
        if not token:
            raise AuthError("missing bearer token")
        token_hash = hash_token(token)
        conn = self.store.connect()
        try:
            row = conn.execute(
                """
                SELECT token_hash, tenant_id, client_id, expires_at, revoked_at
                FROM api_tokens WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            # Compare against a throwaway to keep the not-found path on the same
            # constant-time footing as a real row (no early-exit timing tell).
            hmac.compare_digest(token_hash, token_hash)
            raise AuthError("invalid bearer token")
        if not hmac.compare_digest(str(row["token_hash"]), token_hash):
            raise AuthError("invalid bearer token")
        if row["revoked_at"]:
            raise AuthError("revoked bearer token")
        if _is_expired(row["expires_at"]):
            raise AuthError("expired bearer token")
        return Principal(
            tenant_id=str(row["tenant_id"]),
            client_id=str(row["client_id"] or self.client_id),
        )


def _is_expired(expires_at: object) -> bool:
    if not expires_at:
        return False
    try:
        dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return datetime.now(tz=UTC) > dt
