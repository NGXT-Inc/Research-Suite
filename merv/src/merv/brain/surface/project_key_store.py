"""SQL adapter for Surface-owned project API keys."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from ..kernel.state.store import BaseStateStore, row_to_dict
from ..kernel.utils import NotFoundError
from .project_keys import ProjectKeyRecord


class SqlProjectKeyRepository:
    def __init__(self, *, store: BaseStateStore) -> None:
        self._store = store

    def project_tenant(self, *, project_id: str) -> str:
        with closing(self._store.connect()) as conn:
            row = conn.execute(
                "SELECT tenant_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"project not found: {project_id}")
        return str(row["tenant_id"])

    def insert(self, *, record: ProjectKeyRecord) -> None:
        with self._store.transaction() as conn:
            conn.execute(_INSERT_SQL, _insert_params(record))

    def rotate(self, *, record: ProjectKeyRecord, revoked_at: str) -> bool:
        with self._store.transaction() as conn:
            parent = conn.execute(
                """
                SELECT id FROM project_api_keys
                WHERE id = ? AND project_id = ? AND owner_user_id = ?
                  AND revoked_at IS NULL
                """,
                (record.parent_key_id, record.project_id, record.owner_user_id),
            ).fetchone()
            if parent is None:
                return False
            conn.execute(_INSERT_SQL, _insert_params(record))
            conn.execute(
                "UPDATE project_api_keys SET revoked_at = ? WHERE id = ?",
                (revoked_at, record.parent_key_id),
            )
        return True

    def by_digest(self, *, digest: str) -> ProjectKeyRecord | None:
        with closing(self._store.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM project_api_keys WHERE secret_digest = ?", (digest,)
            ).fetchone()
        return _record(row)

    def by_id(self, *, key_id: str) -> ProjectKeyRecord | None:
        with closing(self._store.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM project_api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return _record(row)

    def list_for_owner(
        self, *, project_id: str, owner_user_id: str
    ) -> list[ProjectKeyRecord]:
        with closing(self._store.connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_api_keys
                WHERE project_id = ? AND owner_user_id = ?
                ORDER BY created_at, id
                """,
                (project_id, owner_user_id),
            ).fetchall()
        return [record for row in rows if (record := _record(row)) is not None]

    def revoke(
        self, *, key_id: str, project_id: str, owner_user_id: str, revoked_at: str
    ) -> ProjectKeyRecord | None:
        with self._store.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM project_api_keys
                WHERE id = ? AND project_id = ? AND owner_user_id = ?
                """,
                (key_id, project_id, owner_user_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE project_api_keys SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ?
                """,
                (revoked_at, key_id),
            )
            updated = conn.execute(
                "SELECT * FROM project_api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return _record(updated)

    def revoke_lineage(
        self, *, key_id: str, project_id: str, owner_user_id: str, revoked_at: str
    ) -> bool:
        with self._store.transaction() as conn:
            root = conn.execute(
                """
                SELECT id FROM project_api_keys
                WHERE id = ? AND project_id = ? AND owner_user_id = ?
                """,
                (key_id, project_id, owner_user_id),
            ).fetchone()
            if root is None:
                return False
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
        return True


_INSERT_SQL = """
INSERT INTO project_api_keys (
  id, secret_digest, owner_user_id, tenant_id, project_id,
  audience, oauth_family_id, created_at, expires_at, revoked_at, parent_key_id,
  sandbox_seconds_ceiling, blob_bytes_ceiling
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_params(record: ProjectKeyRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.secret_digest,
        record.owner_user_id,
        record.tenant_id,
        record.project_id,
        record.audience,
        record.oauth_family_id,
        record.created_at,
        record.expires_at,
        record.revoked_at,
        record.parent_key_id,
        record.sandbox_seconds_ceiling,
        record.blob_bytes_ceiling,
    )


def _record(row: Any) -> ProjectKeyRecord | None:
    data = row_to_dict(row=row)
    if data is None:
        return None
    return ProjectKeyRecord(
        id=str(data["id"]),
        secret_digest=str(data["secret_digest"]),
        owner_user_id=str(data["owner_user_id"]),
        tenant_id=str(data["tenant_id"]),
        project_id=str(data["project_id"]),
        audience=str(data["audience"]) if data.get("audience") else None,
        oauth_family_id=(
            str(data["oauth_family_id"]) if data.get("oauth_family_id") else None
        ),
        created_at=str(data["created_at"]),
        expires_at=str(data["expires_at"]) if data.get("expires_at") else None,
        revoked_at=str(data["revoked_at"]) if data.get("revoked_at") else None,
        parent_key_id=(
            str(data["parent_key_id"]) if data.get("parent_key_id") else None
        ),
        sandbox_seconds_ceiling=(
            int(data["sandbox_seconds_ceiling"])
            if data.get("sandbox_seconds_ceiling") is not None
            else None
        ),
        blob_bytes_ceiling=(
            int(data["blob_bytes_ceiling"])
            if data.get("blob_bytes_ceiling") is not None
            else None
        ),
    )


__all__ = ["SqlProjectKeyRepository"]
