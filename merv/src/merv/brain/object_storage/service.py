"""Project ledger for durable heavy-file storage."""

from __future__ import annotations

import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any

from merv.shared.file_transfer import (
    download_target_to_file,
    file_digest,
    upload_file_to_target,
)
from merv.shared.storage_guidance import storage_guidance

from ..kernel.ports.object_store import ObjectStore
from .blobs import _validate_keys
from ..kernel.state.store import (
    BaseStateStore,
    Connection,
    Row,
    next_created_seq,
    row_to_dict,
)
from ..kernel.utils import (
    NotFoundError,
    ValidationError,
    format_iso,
    iso_after,
    new_id,
    now_iso,
)


STORAGE_KINDS = {"dataset", "model", "other"}
STORAGE_STATUSES = {"uploading", "completing", "available", "expired", "deleted"}
STORAGE_DEFAULT_TTL_SECONDS = 60 * 24 * 3600
PRESIGN_TTL_SECONDS = 3600


def objects_for_experiment(
    *, conn: Connection, project_id: str, experiment_id: str
) -> list[dict[str, Any]]:
    """Ledger rows produced by one experiment; injected into research_core."""
    rows = conn.execute(
        """
        SELECT id, name, version, kind, content_sha256, size_bytes,
               content_type, status, expires_at, producing_run, source_uri,
               notes, created_at, updated_at, last_accessed_at
        FROM storage_objects
        WHERE project_id = ? AND producing_experiment_id = ?
          AND status != 'deleted'
        ORDER BY kind, name, version DESC, created_seq DESC
        """,
        (project_id, experiment_id),
    ).fetchall()
    return [row_to_dict(row=row) or {} for row in rows]


class StorageLedgerService:
    """Ledger + lifecycle owner for project-scoped heavy objects."""

    def __init__(self, *, store: BaseStateStore, objects: ObjectStore) -> None:
        self.store = store
        self.objects = objects

    def put_object(
        self,
        *,
        project_id: str | None,
        name: str,
        kind: str,
        sha256: str,
        size_bytes: int,
        content_type: str = "application/octet-stream",
        created_by: str = "codex",
        producing_experiment_id: str = "",
        producing_run: str = "",
        source_uri: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        self._validate_kind(kind)
        self._validate_name(name)
        content_type = content_type or "application/octet-stream"
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            namespace = self._namespace(project_id=project_id)
            _validate_keys(namespace=namespace, sha256=sha256)
            existing = conn.execute(
                """
                SELECT *
                FROM storage_objects
                WHERE project_id = ? AND name = ? AND content_sha256 = ?
                  AND status = 'available'
                ORDER BY version DESC, created_seq DESC
                LIMIT 1
                """,
                (project_id, name, sha256),
            ).fetchone()
            if existing is not None:
                return {
                    "deduped": False,
                    "idempotent": True,
                    "object": self._hydrate(row=existing),
                }

            version = self._next_version(conn=conn, project_id=project_id, name=name)
            stat = self.objects.stat(namespace=namespace, sha256=sha256)
            if stat is not None:
                registered_size = int(stat.size_bytes)
                registered_content_type = str(stat.content_type or content_type)
                status = "available"
                upload_id = None
                expires_at = iso_after(seconds=STORAGE_DEFAULT_TTL_SECONDS)
            else:
                upload = self.objects.presign_upload(
                    namespace=namespace,
                    sha256=sha256,
                    size_bytes=int(size_bytes),
                    content_type=content_type,
                    expires_in=PRESIGN_TTL_SECONDS,
                )
                registered_size = int(size_bytes)
                registered_content_type = content_type
                status = "uploading"
                upload_id = str(upload["upload_id"])
                expires_at = None
            row = self._insert_object(
                conn=conn,
                project_id=project_id,
                name=name,
                version=version,
                kind=kind,
                sha256=sha256,
                size_bytes=registered_size,
                content_type=registered_content_type,
                namespace=namespace,
                status=status,
                upload_id=upload_id,
                expires_at=expires_at,
                created_by=created_by,
                producing_experiment_id=producing_experiment_id,
                producing_run=producing_run,
                source_uri=source_uri,
                notes=notes,
            )
            self._record(
                conn=conn,
                project_id=project_id,
                event_type="storage.registered",
                row=row,
            )
            if stat is not None:
                return {"deduped": True, "object": self._hydrate(row=row)}
            return {"object": self._hydrate(row=row), "upload": upload}

    def upload_file(
        self,
        *,
        project_id: str | None,
        path: str | Path,
        name: str,
        kind: str,
        content_type: str = "",
        created_by: str = "codex",
        producing_experiment_id: str = "",
        producing_run: str = "",
        source_uri: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        """Register, upload, and complete a local file in one call."""
        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise ValidationError(f"storage upload file not found: {file_path}")
        if not file_path.is_file():
            raise ValidationError(f"storage upload path is not a file: {file_path}")
        sha256, size_bytes = file_digest(file_path)
        resolved_content_type = (
            content_type
            or mimetypes.guess_type(str(file_path))[0]
            or "application/octet-stream"
        )
        registered = self.put_object(
            project_id=project_id,
            name=name,
            kind=kind,
            sha256=sha256,
            size_bytes=size_bytes,
            content_type=resolved_content_type,
            created_by=created_by,
            producing_experiment_id=producing_experiment_id,
            producing_run=producing_run,
            source_uri=source_uri,
            notes=notes,
        )
        result: dict[str, Any] = {
            key: value for key, value in registered.items() if key != "upload"
        }
        result["path"] = str(file_path)
        result["sha256"] = sha256
        result["size_bytes"] = size_bytes
        result["uploaded"] = False
        upload = registered.get("upload")
        if not upload:
            return result
        completed_parts = upload_file_to_target(
            upload=upload,
            file_path=file_path,
            size_bytes=size_bytes,
            content_type=resolved_content_type,
        )
        completed = self.complete_upload(
            project_id=project_id,
            upload_id=str(upload["upload_id"]),
            parts=completed_parts,
        )
        result["object"] = completed
        result["uploaded"] = True
        return result

    def download_file(
        self,
        *,
        project_id: str | None,
        path: str | Path,
        object_id: str | None = None,
        name: str | None = None,
        version: int | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Resolve a storage object and write it to a local file."""
        target = Path(path).expanduser()
        if target.exists() and not overwrite:
            raise ValidationError(
                f"download target already exists; pass overwrite=true to replace: {target}"
            )
        resolved = self.resolve(
            project_id=project_id,
            object_id=object_id,
            name=name,
            version=version,
            include_download=True,
        )
        obj = resolved["object"]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp-{new_id(prefix='download')}")
        try:
            download_target_to_file(download=resolved["download"], path=tmp)
            sha256, size_bytes = file_digest(tmp)
            if sha256 != str(obj["content_sha256"]):
                raise ValidationError(
                    "downloaded storage object checksum mismatch: "
                    f"{sha256} != {obj['content_sha256']}"
                )
            if size_bytes != int(obj["size_bytes"]):
                raise ValidationError(
                    "downloaded storage object size mismatch: "
                    f"{size_bytes} != {obj['size_bytes']} bytes"
                )
            tmp.replace(target)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return {
            "object": obj,
            "path": str(target),
            "downloaded": True,
            "bytes_written": int(obj["size_bytes"]),
        }

    def complete_upload(
        self,
        *,
        project_id: str | None,
        upload_id: str,
        parts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            # Reserve the row before touching bytes so delete cannot orphan a completion.
            cursor = conn.execute(
                """
                UPDATE storage_objects
                SET status = 'completing', updated_at = ?
                WHERE project_id = ? AND upload_id = ? AND status = 'uploading'
                """,
                (now_iso(), project_id, upload_id),
            )
            if int(getattr(cursor, "rowcount", 0)) != 1:
                raise NotFoundError(
                    f"upload not found in project {project_id}: {upload_id}"
                )
            row = self._get_by_upload(
                conn=conn, project_id=project_id, upload_id=upload_id
            )
        try:
            stat = self.objects.complete_upload(upload_id=upload_id, parts=parts)
        except Exception:
            with self.store.transaction() as conn:
                project_id = self.store.require_project_id(
                    conn=conn, project_id=project_id
                )
                conn.execute(
                    """
                    UPDATE storage_objects
                    SET status = 'uploading', updated_at = ?
                    WHERE project_id = ? AND upload_id = ? AND status = 'completing'
                    """,
                    (now_iso(), project_id, upload_id),
                )
            raise
        if str(stat.namespace) != str(row["namespace"]) or str(stat.sha256) != str(
            row["content_sha256"]
        ):
            raise ValidationError(
                f"upload {upload_id} completed with unexpected object identity"
            )
        now = now_iso()
        expires_at = iso_after(seconds=STORAGE_DEFAULT_TTL_SECONDS)
        try:
            with self.store.transaction() as conn:
                project_id = self.store.require_project_id(
                    conn=conn, project_id=project_id
                )
                cursor = conn.execute(
                    """
                    UPDATE storage_objects
                    SET status = 'available', size_bytes = ?, content_type = ?,
                        expires_at = ?, last_accessed_at = ?, updated_at = ?
                    WHERE project_id = ? AND upload_id = ? AND status = 'completing'
                    """,
                    (
                        int(stat.size_bytes),
                        str(stat.content_type or "application/octet-stream"),
                        expires_at,
                        now,
                        now,
                        project_id,
                        upload_id,
                    ),
                )
                if int(getattr(cursor, "rowcount", 0)) != 1:
                    raise NotFoundError(
                        f"upload not completing in project {project_id}: {upload_id}"
                    )
                updated = self._get_by_upload(
                    conn=conn, project_id=project_id, upload_id=upload_id
                )
                self._record(
                    conn=conn,
                    project_id=project_id,
                    event_type="storage.completed",
                    row=updated,
                )
                return self._hydrate(row=updated)
        except Exception:
            self._reclaim_if_unreferenced_after_commit(
                namespace=str(row["namespace"]),
                sha256=str(row["content_sha256"]),
            )
            raise

    def list_objects(
        self,
        *,
        project_id: str | None,
        kind: str | None = None,
        name: str | None = None,
        status: str | None = None,
        include_expired: bool = False,
        limit: int | None = None,
        offset: int = 0,
        compact: bool = False,
    ) -> dict[str, Any]:
        if kind is not None:
            self._validate_kind(kind)
        if status is not None:
            self._validate_status(status)
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            where = ["project_id = ?"]
            params: list[Any] = [project_id]
            if kind:
                where.append("kind = ?")
                params.append(kind)
            if name:
                where.append("name = ?")
                params.append(name)
            if status:
                where.append("status = ?")
                params.append(status)
            else:
                where.append(
                    "status IN ('available', 'expired')"
                    if include_expired
                    else "status = 'available'"
                )
            base = f"FROM storage_objects WHERE {' AND '.join(where)}"
            total_row = conn.execute(
                f"SELECT COUNT(*) AS total {base}", params
            ).fetchone()
            total = int(total_row["total"] if total_row is not None else 0)
            query = f"SELECT * {base} ORDER BY name, version DESC, created_seq DESC"
            page_params = list(params)
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                page_params += [int(limit), int(offset)]
            elif offset:
                query += " LIMIT ? OFFSET ?"
                page_params += [2_147_483_647, int(offset)]
            rows = conn.execute(query, page_params).fetchall()
            objects = [self._hydrate(row=row, compact=compact) for row in rows]
            returned = len(objects)
            return {
                "objects": objects,
                "count": returned,
                "returned": returned,
                "total": total,
                "offset": int(offset),
                "has_more": (int(offset) + returned) < total,
                "compact": bool(compact),
                "guidance": storage_guidance(enabled=True),
            }
        finally:
            conn.close()

    def get_object(self, *, project_id: str | None, object_id: str) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            return {
                "object": self._hydrate(
                    row=self._get_by_id(
                        conn=conn, project_id=project_id, object_id=object_id
                    )
                )
            }
        finally:
            conn.close()

    def resolve(
        self,
        *,
        project_id: str | None,
        object_id: str | None = None,
        name: str | None = None,
        version: int | None = None,
        include_download: bool = True,
    ) -> dict[str, Any]:
        if bool(object_id) == bool(name):
            raise ValidationError("provide exactly one of object_id or name")
        now = now_iso()
        next_expiry = iso_after(seconds=STORAGE_DEFAULT_TTL_SECONDS)
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = self._resolve_row(
                conn=conn,
                project_id=project_id,
                object_id=object_id,
                name=name,
                version=version,
            )
            if row is None or str(row["status"]) != "available":
                target = (
                    object_id
                    if object_id
                    else (f"{name}@{version}" if version is not None else name)
                )
                raise NotFoundError(
                    f"storage object not available in project {project_id}: {target}"
                )
            expires_at = row["expires_at"]
            if expires_at is not None and str(next_expiry) > str(expires_at):
                conn.execute(
                    """
                    UPDATE storage_objects
                    SET expires_at = ?, last_accessed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_expiry, now, now, row["id"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE storage_objects
                    SET last_accessed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, row["id"]),
                )
            row = self._get_by_id(
                conn=conn, project_id=project_id, object_id=str(row["id"])
            )
            obj = self._hydrate(row=row)
        result: dict[str, Any] = {"object": obj}
        if include_download:
            result["download"] = self.objects.presign_download(
                namespace=str(obj["namespace"]),
                sha256=str(obj["content_sha256"]),
                expires_in=PRESIGN_TTL_SECONDS,
            )
        return result

    def pin(self, *, project_id: str | None, object_id: str) -> dict[str, Any]:
        return self._set_expiry(
            project_id=project_id, object_id=object_id, expires_at=None
        )

    def unpin(self, *, project_id: str | None, object_id: str) -> dict[str, Any]:
        return self._set_expiry(
            project_id=project_id,
            object_id=object_id,
            expires_at=iso_after(seconds=STORAGE_DEFAULT_TTL_SECONDS),
        )

    def renew(self, *, project_id: str | None, object_id: str) -> dict[str, Any]:
        return self.unpin(project_id=project_id, object_id=object_id)

    def delete(self, *, project_id: str | None, object_id: str) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = self._get_by_id(conn=conn, project_id=project_id, object_id=object_id)
            if str(row["status"]) == "deleted":
                return {
                    "deleted": False,
                    "reclaimed": False,
                    "object": self._hydrate(row=row),
                }
            if str(row["status"]) == "completing":
                raise ValidationError(
                    f"storage object is completing and cannot be deleted: {object_id}"
                )
            now = now_iso()
            conn.execute(
                "UPDATE storage_objects SET status = 'deleted', updated_at = ? WHERE id = ?",
                (now, object_id),
            )
            updated = self._get_by_id(
                conn=conn, project_id=project_id, object_id=object_id
            )
            self._record(
                conn=conn,
                project_id=project_id,
                event_type="storage.deleted",
                row=updated,
            )
            obj = self._hydrate(row=updated)
            namespace = str(row["namespace"])
            sha256 = str(row["content_sha256"])
        reclaimed = self._reclaim_if_unreferenced_after_commit(
            namespace=namespace, sha256=sha256
        )
        return {"deleted": True, "reclaimed": reclaimed, "object": obj}

    def sweep_expired(self, *, now: str | datetime | None = None) -> int:
        cutoff = self._cutoff(now=now)
        swept = 0
        freed: list[tuple[str, str]] = []
        with self.store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM storage_objects
                WHERE status = 'available' AND expires_at IS NOT NULL AND expires_at <= ?
                ORDER BY created_seq
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE storage_objects SET status = 'expired', updated_at = ? WHERE id = ?",
                    (cutoff, row["id"]),
                )
                updated = self._get_by_id(
                    conn=conn,
                    project_id=str(row["project_id"]),
                    object_id=str(row["id"]),
                )
                self._record(
                    conn=conn,
                    project_id=str(row["project_id"]),
                    event_type="storage.expired",
                    row=updated,
                )
                freed.append((str(row["namespace"]), str(row["content_sha256"])))
                swept += 1
        for namespace, sha256 in freed:
            self._reclaim_if_unreferenced_after_commit(
                namespace=namespace, sha256=sha256
            )
        return swept

    def _insert_object(
        self,
        *,
        conn: Connection,
        project_id: str,
        name: str,
        version: int,
        kind: str,
        sha256: str,
        size_bytes: int,
        content_type: str,
        namespace: str,
        status: str,
        upload_id: str | None,
        expires_at: str | None,
        created_by: str,
        producing_experiment_id: str,
        producing_run: str,
        source_uri: str,
        notes: str,
    ) -> Row:
        now = now_iso()
        object_id = new_id(prefix="sto")
        conn.execute(
            """
            INSERT INTO storage_objects (
              id, project_id, name, version, kind, content_sha256, size_bytes,
              content_type, namespace, status, upload_id, expires_at, created_by,
              producing_experiment_id, producing_run, source_uri, notes,
              created_at, updated_at, last_accessed_at, created_seq
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                object_id,
                project_id,
                name,
                int(version),
                kind,
                sha256,
                int(size_bytes),
                content_type,
                namespace,
                status,
                upload_id,
                expires_at,
                created_by,
                producing_experiment_id,
                producing_run,
                source_uri,
                notes,
                now,
                now,
                next_created_seq(conn=conn, table="storage_objects"),
            ),
        )
        return self._get_by_id(conn=conn, project_id=project_id, object_id=object_id)

    def _set_expiry(
        self, *, project_id: str | None, object_id: str, expires_at: str | None
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            self._get_by_id(conn=conn, project_id=project_id, object_id=object_id)
            conn.execute(
                "UPDATE storage_objects SET expires_at = ?, updated_at = ? WHERE id = ?",
                (expires_at, now_iso(), object_id),
            )
            return self._hydrate(
                row=self._get_by_id(
                    conn=conn, project_id=project_id, object_id=object_id
                )
            )

    def _resolve_row(
        self,
        *,
        conn: Connection,
        project_id: str,
        object_id: str | None,
        name: str | None,
        version: int | None,
    ) -> Row | None:
        if object_id:
            return conn.execute(
                "SELECT * FROM storage_objects WHERE project_id = ? AND id = ?",
                (project_id, object_id),
            ).fetchone()
        if version is None:
            return conn.execute(
                """
                SELECT *
                FROM storage_objects
                WHERE project_id = ? AND name = ? AND status = 'available'
                ORDER BY version DESC, created_seq DESC
                LIMIT 1
                """,
                (project_id, name),
            ).fetchone()
        return conn.execute(
            """
            SELECT *
            FROM storage_objects
            WHERE project_id = ? AND name = ? AND version = ?
            """,
            (project_id, name, int(version)),
        ).fetchone()

    def _get_by_id(self, *, conn: Connection, project_id: str, object_id: str) -> Row:
        row = conn.execute(
            "SELECT * FROM storage_objects WHERE project_id = ? AND id = ?",
            (project_id, object_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"storage object not found in project {project_id}: {object_id}"
            )
        return row

    def _get_by_upload(
        self, *, conn: Connection, project_id: str, upload_id: str
    ) -> Row:
        row = conn.execute(
            "SELECT * FROM storage_objects WHERE project_id = ? AND upload_id = ?",
            (project_id, upload_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"upload not found in project {project_id}: {upload_id}"
            )
        return row

    def _next_version(self, *, conn: Connection, project_id: str, name: str) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM storage_objects
            WHERE project_id = ? AND name = ?
            """,
            (project_id, name),
        ).fetchone()
        return int(row["next_version"])

    def _reclaim_if_unreferenced_after_commit(
        self, *, namespace: str, sha256: str
    ) -> bool:
        with self.store.transaction() as conn:
            return self._reclaim_if_unreferenced(
                conn=conn, namespace=namespace, sha256=sha256
            )

    def _reclaim_if_unreferenced(
        self, *, conn: Connection, namespace: str, sha256: str
    ) -> bool:
        remaining = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM storage_objects
            WHERE namespace = ? AND content_sha256 = ?
              AND status IN ('uploading', 'completing', 'available')
            """,
            (namespace, sha256),
        ).fetchone()
        if int(remaining["count"]) > 0:
            return False
        return self.objects.delete(namespace=namespace, sha256=sha256)

    def _record(
        self, *, conn: Connection, project_id: str, event_type: str, row: Row
    ) -> None:
        self.store.record_event(
            conn=conn,
            project_id=project_id,
            event_type=event_type,
            target_type="storage_object",
            target_id=str(row["id"]),
            payload={
                "name": row["name"],
                "version": int(row["version"]),
                "sha256": row["content_sha256"],
                "status": row["status"],
            },
        )

    def _namespace(self, *, project_id: str) -> str:
        # Tenant-prefixing belongs to Phase 3 composition/config wiring.
        return project_id

    def _hydrate(self, *, row: Row, compact: bool = False) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        if compact:
            fields = (
                "id",
                "project_id",
                "name",
                "version",
                "kind",
                "content_sha256",
                "size_bytes",
                "status",
                "expires_at",
                "updated_at",
            )
            return {key: data.get(key) for key in fields}
        return data

    def _cutoff(self, *, now: str | datetime | None) -> str:
        if isinstance(now, datetime):
            return format_iso(now)
        return str(now) if now is not None else now_iso()

    def _validate_kind(self, kind: str) -> None:
        if kind not in STORAGE_KINDS:
            raise ValidationError(
                f"invalid storage kind: {kind}; allowed: {', '.join(sorted(STORAGE_KINDS))}"
            )

    def _validate_status(self, status: str) -> None:
        if status not in STORAGE_STATUSES:
            raise ValidationError(
                f"invalid storage status: {status}; allowed: {', '.join(sorted(STORAGE_STATUSES))}"
            )

    def _validate_name(self, name: str) -> None:
        if not name:
            raise ValidationError("storage object name is required")


__all__ = [
    "PRESIGN_TTL_SECONDS",
    "STORAGE_DEFAULT_TTL_SECONDS",
    "STORAGE_KINDS",
    "StorageLedgerService",
]
