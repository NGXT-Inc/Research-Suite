"""SQL adapter for Surface-owned OAuth state."""

from __future__ import annotations

import json
from contextlib import closing
from typing import Any

from ..kernel.state.store import BaseStateStore, row_to_dict
from .oauth import AuthorizationCode, OAuthClient, RefreshToken


class SqlOAuthRepository:
    def __init__(self, *, store: BaseStateStore) -> None:
        self._store = store

    def insert_client(self, *, client: OAuthClient) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO oauth_clients (
                  client_id, client_name, redirect_uris_json, grant_types_json,
                  created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    client.client_id,
                    client.client_name,
                    json.dumps(list(client.redirect_uris), separators=(",", ":")),
                    json.dumps(list(client.grant_types), separators=(",", ":")),
                    client.created_at,
                ),
            )

    def client_by_id(self, *, client_id: str) -> OAuthClient | None:
        with closing(self._store.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        data = row_to_dict(row=row)
        if data is None:
            return None
        return OAuthClient(
            client_id=str(data["client_id"]),
            client_name=str(data["client_name"]),
            redirect_uris=tuple(json.loads(str(data["redirect_uris_json"]))),
            grant_types=tuple(json.loads(str(data["grant_types_json"]))),
            created_at=str(data["created_at"]),
        )

    def insert_code(self, *, code: AuthorizationCode) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO oauth_authorization_codes (
                  code_digest, client_id, redirect_uri, owner_user_id, project_id,
                  code_challenge, resource, created_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code.code_digest,
                    code.client_id,
                    code.redirect_uri,
                    code.owner_user_id,
                    code.project_id,
                    code.code_challenge,
                    code.resource,
                    code.created_at,
                    code.expires_at,
                    code.consumed_at,
                ),
            )

    def code_by_digest(self, *, digest: str) -> AuthorizationCode | None:
        with closing(self._store.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM oauth_authorization_codes WHERE code_digest = ?",
                (digest,),
            ).fetchone()
        data = row_to_dict(row=row)
        if data is None:
            return None
        return AuthorizationCode(
            code_digest=str(data["code_digest"]),
            client_id=str(data["client_id"]),
            redirect_uri=str(data["redirect_uri"]),
            owner_user_id=str(data["owner_user_id"]),
            project_id=str(data["project_id"]),
            code_challenge=str(data["code_challenge"]),
            resource=str(data["resource"]),
            created_at=str(data["created_at"]),
            expires_at=str(data["expires_at"]),
            consumed_at=(str(data["consumed_at"]) if data.get("consumed_at") else None),
        )

    def consume_code(self, *, digest: str, consumed_at: str) -> bool:
        with self._store.transaction() as conn:
            row = conn.execute(
                """
                SELECT code_digest FROM oauth_authorization_codes
                WHERE code_digest = ? AND consumed_at IS NULL AND expires_at > ?
                """,
                (digest, consumed_at),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE oauth_authorization_codes SET consumed_at = ?
                WHERE code_digest = ? AND consumed_at IS NULL
                """,
                (consumed_at, digest),
            )
        return True

    def insert_refresh_token(self, *, token: RefreshToken) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO oauth_refresh_tokens (
                  id, family_id, secret_digest, client_id, owner_user_id, project_id,
                  resource, current_key_id, parent_token_id, created_at,
                  expires_at, consumed_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token.id,
                    token.family_id,
                    token.secret_digest,
                    token.client_id,
                    token.owner_user_id,
                    token.project_id,
                    token.resource,
                    token.current_key_id,
                    token.parent_token_id,
                    token.created_at,
                    token.expires_at,
                    token.consumed_at,
                    token.revoked_at,
                ),
            )

    def refresh_token_by_digest(self, *, digest: str) -> RefreshToken | None:
        with closing(self._store.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM oauth_refresh_tokens WHERE secret_digest = ?",
                (digest,),
            ).fetchone()
        return _refresh_token(row)

    def consume_refresh_token(self, *, token_id: str, consumed_at: str) -> bool:
        with self._store.transaction() as conn:
            row = conn.execute(
                """
                SELECT r.id FROM oauth_refresh_tokens r
                JOIN project_api_keys k ON k.id = r.current_key_id
                WHERE r.id = ? AND r.consumed_at IS NULL AND r.revoked_at IS NULL
                  AND r.expires_at > ? AND k.revoked_at IS NULL
                """,
                (token_id, consumed_at),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE oauth_refresh_tokens SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (consumed_at, token_id),
            )
        return True

    def revoke_refresh_family_and_key_lineage(
        self,
        *,
        family_id: str,
        key_id: str,
        project_id: str,
        owner_user_id: str,
        revoked_at: str,
    ) -> None:
        """Revoke replay authority and every derived bearer in one commit."""
        with self._store.transaction() as conn:
            conn.execute(
                """
                UPDATE oauth_refresh_tokens
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE family_id = ?
                """,
                (revoked_at, family_id),
            )
            conn.execute(
                """
                WITH RECURSIVE lineage(id) AS (
                  SELECT id FROM project_api_keys WHERE id = ?
                  UNION ALL
                  SELECT child.id FROM project_api_keys child
                  JOIN lineage parent ON child.parent_key_id = parent.id
                )
                UPDATE project_api_keys SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id IN (SELECT id FROM lineage)
                  AND project_id = ? AND owner_user_id = ?
                """,
                (key_id, revoked_at, project_id, owner_user_id),
            )


def _refresh_token(row: Any) -> RefreshToken | None:
    data = row_to_dict(row=row)
    if data is None:
        return None
    return RefreshToken(
        id=str(data["id"]),
        family_id=str(data["family_id"]),
        secret_digest=str(data["secret_digest"]),
        client_id=str(data["client_id"]),
        owner_user_id=str(data["owner_user_id"]),
        project_id=str(data["project_id"]),
        resource=str(data["resource"]),
        current_key_id=str(data["current_key_id"]),
        parent_token_id=(
            str(data["parent_token_id"]) if data.get("parent_token_id") else None
        ),
        created_at=str(data["created_at"]),
        expires_at=str(data["expires_at"]),
        consumed_at=(str(data["consumed_at"]) if data.get("consumed_at") else None),
        revoked_at=(str(data["revoked_at"]) if data.get("revoked_at") else None),
    )


__all__ = ["SqlOAuthRepository"]
