"""Surface-owned quota join for the heavy-object ledger."""

from __future__ import annotations

from typing import Any

from ..utils import PermissionDeniedError


class StorageQuotaService:
    """Admits byte reservations without coupling object storage to sandbox quotas."""

    def check_reservation(
        self,
        *,
        conn: Any,
        project_id: str,
        sha256: str,
        size_bytes: int,
    ) -> None:
        quota = conn.execute(
            """
            SELECT p.tenant_id, q.blob_bytes_budget
            FROM projects p
            LEFT JOIN tenant_quotas q ON q.tenant_id = p.tenant_id
            WHERE p.id = ?
            """,
            (project_id,),
        ).fetchone()
        if quota is None or quota["blob_bytes_budget"] is None:
            return
        reserved = conn.execute(
            """
            SELECT COALESCE(SUM(size_bytes), 0) AS size_bytes
            FROM (
              SELECT o.size_bytes
              FROM storage_objects o
              JOIN projects p ON p.id = o.project_id
              WHERE p.tenant_id = ?
                AND o.status IN ('uploading', 'completing')
              UNION ALL
              SELECT MAX(o.size_bytes) AS size_bytes
              FROM storage_objects o
              JOIN projects p ON p.id = o.project_id
              WHERE p.tenant_id = ?
                AND o.status = 'available'
              GROUP BY o.project_id, o.content_sha256
            ) AS objects
            """,
            (quota["tenant_id"], quota["tenant_id"]),
        ).fetchone()
        existing = conn.execute(
            """
            SELECT COALESCE(MAX(size_bytes), 0) AS size_bytes
            FROM storage_objects
            WHERE project_id = ? AND content_sha256 = ?
              AND status = 'available'
            """,
            (project_id, sha256),
        ).fetchone()
        used = int(reserved["size_bytes"])
        additional = max(0, int(size_bytes) - int(existing["size_bytes"]))
        if additional == 0:
            return
        limit = int(quota["blob_bytes_budget"])
        if used + additional > limit:
            raise PermissionDeniedError(
                f"tenant blob byte budget exceeded ({used + additional}/{limit} bytes)",
                details={
                    "quota": "blob_bytes_budget",
                    "limit": limit,
                    "used": used,
                    "requested": additional,
                },
            )


__all__ = ["StorageQuotaService"]
