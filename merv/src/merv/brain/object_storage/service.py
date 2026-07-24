"""Project ledger for durable heavy-file storage."""

from __future__ import annotations

import base64
import secrets
from contextlib import closing
from datetime import datetime
from typing import Any

from merv.shared.storage_guidance import storage_guidance

from ..kernel.ports.blob_store import validate_blob_keys
from ..kernel.ports.object_store import ObjectStore
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
# S3's hard per-object single-PUT limit. storage.submit's token-curl command is
# one presigned PUT, so anything larger is rejected (multipart orchestration is a
# documented v1 non-goal).
SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 * 1024
# Absolute server-side upload ceiling; composition overrides from the env.
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024 * 1024
# One-time completion token lifetime — outlives the 1-hour presigned PUT window
# plus the trailing completion POST.
COMPLETION_TOKEN_TTL_SECONDS = PRESIGN_TTL_SECONDS + 3600
_LOCAL_API_BASE = "http://127.0.0.1:8787"


def _shell_quote(value: str) -> str:
    """POSIX single-quote — the agent runs the command verbatim in a shell."""
    return "'" + value.replace("'", "'\\''") + "'"


def _checksum_sha256_b64(sha256: str) -> str:
    """base64(SHA-256 raw bytes): the value S3 binds into the presigned PUT and
    the agent must echo in the x-amz-checksum-sha256 header."""
    return base64.b64encode(bytes.fromhex(sha256)).decode("ascii")


def storage_submit_command(
    *, base_url: str, path: str, presigned_url: str, checksum_b64: str,
    content_type: str, token: str
) -> str:
    """Compound one-liner: push the bytes straight to S3, then finalize the
    ledger object through the auth-exempt completion token. Bytes go direct to
    S3 — never through the brain. Both the checksum AND the Content-Type are
    bound into the presigned PUT's SigV4 signature, so the curl MUST send both
    headers verbatim or S3 rejects the upload with SignatureDoesNotMatch."""
    base = (base_url or _LOCAL_API_BASE).rstrip("/")
    put = (
        f"curl -sf -X PUT -H 'x-amz-checksum-sha256:{checksum_b64}' "
        f"-H 'Content-Type: {content_type}' "
        f"-T {_shell_quote(path)} {_shell_quote(presigned_url)}"
    )
    complete = f"curl -sf -X POST '{base}/api/storage/u/{token}/complete'"
    return f"{put} && {complete}"


def storage_fetch_command(*, path: str, presigned_url: str, sha256: str) -> str:
    """Download straight from S3, then verify the sha256 the ledger already
    holds. Zero new server capability."""
    fetch = f"curl -sf -o {_shell_quote(path)} {_shell_quote(presigned_url)}"
    verify = f"printf '%s  %s\\n' {sha256} {_shell_quote(path)} | shasum -a 256 -c"
    return f"{fetch} && {verify}"


class StorageLedgerService:
    """Ledger + lifecycle owner for project-scoped heavy objects."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        objects: ObjectStore,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ) -> None:
        self.store = store
        self.objects = objects
        self.max_upload_bytes = int(max_upload_bytes)

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
            validate_blob_keys(namespace=namespace, sha256=sha256)
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

    def submit(
        self,
        *,
        project_id: str | None,
        path: str,
        kind: str,
        sha256: str,
        size_bytes: int,
        name: str = "",
        content_type: str = "",
        created_by: str = "agent",
        producing_experiment_id: str = "",
        producing_run: str = "",
        source_uri: str = "",
        notes: str = "",
        base_url: str = "",
    ) -> dict[str, Any]:
        """Register a heavy object and return the token-curl command that pushes
        its bytes straight to S3 and finalizes the ledger row.

        The advisory client sha feeds the existing name+sha dedup; identity is
        still enforced server-side by the presigned checksum and the completion
        head-verify. Bytes never transit the brain (fatal for multi-GB)."""
        if not str(path).strip():
            raise ValidationError("path is required (the local file to upload)")
        self._enforce_upload_size(size_bytes=int(size_bytes))
        content_type = content_type or "application/octet-stream"
        registered = self.put_object(
            project_id=project_id,
            name=str(name).strip() or str(path).strip(),
            kind=kind,
            sha256=sha256,
            size_bytes=int(size_bytes),
            content_type=content_type,
            created_by=created_by,
            producing_experiment_id=producing_experiment_id,
            producing_run=producing_run,
            source_uri=source_uri,
            notes=notes,
        )
        obj = registered["object"]
        upload = registered.get("upload")
        if upload is None:
            # The advisory sha matched content already present (name+sha or
            # physical dedup): the object is available, nothing to upload.
            return {
                "object": obj,
                "uploaded": True,
                "deduped": bool(registered.get("deduped")),
                "idempotent": bool(registered.get("idempotent")),
                "run": "",
            }
        if "url" not in upload:
            # The single-PUT command cannot drive a multipart presign; the size
            # guard should have caught this, so only a store configured with a
            # sub-5 GiB multipart threshold reaches here.
            raise ValidationError(
                "this object needs a multipart upload, unsupported by the v1 "
                "token-curl command — reduce the file below the single-PUT "
                "ceiling or configure the store for single-PUT",
                details={"size_bytes": int(size_bytes)},
            )
        token = self._mint_completion_token(
            project_id=str(obj["project_id"]),
            object_id=str(obj["id"]),
            upload_id=str(upload["upload_id"]),
        )
        run = storage_submit_command(
            base_url=base_url,
            path=str(path),
            presigned_url=str(upload["url"]),
            checksum_b64=_checksum_sha256_b64(sha256),
            content_type=str(upload.get("content_type") or content_type),
            token=token,
        )
        return {
            "object": obj,
            "upload_id": str(upload["upload_id"]),
            "uploaded": False,
            "run": run,
        }

    def fetch(
        self,
        *,
        project_id: str | None,
        path: str,
        object_id: str | None = None,
        name: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        """Resolve a storage object and return the curl-download + sha256-verify
        command, built entirely from the ledger row (content_sha256 is already
        stored). Zero new server capability."""
        if not str(path).strip():
            raise ValidationError("path is required (the local destination file)")
        resolved = self.resolve(
            project_id=project_id,
            object_id=object_id,
            name=name,
            version=version,
            include_download=True,
        )
        obj = resolved["object"]
        run = storage_fetch_command(
            path=str(path),
            presigned_url=str(resolved["download"]["url"]),
            sha256=str(obj["content_sha256"]),
        )
        return {"object": obj, "run": run}

    def complete_via_token(self, *, token: str) -> dict[str, Any]:
        """Finalize a pending upload named by a one-time completion token.

        Token-first: an unknown/expired/consumed token raises NotFoundError (404)
        before any object work. Single-use: the row is deleted once the
        head-verify completion succeeds, so a transient pre-upload failure can be
        retried within the TTL. This is the ONLY wire-reachable completion for a
        key agent — storage.complete_upload stays internal + MCP-403'd."""
        self._sweep_completion_tokens()
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                """
                SELECT project_id, upload_id
                FROM storage_completion_tokens
                WHERE token = ? AND status = 'pending' AND expires_at > ?
                """,
                (token, now_iso()),
            ).fetchone()
        if row is None:
            raise NotFoundError("unknown, used, or expired storage completion token")
        completed = self.complete_upload(
            project_id=str(row["project_id"]), upload_id=str(row["upload_id"])
        )
        with self.store.transaction() as conn:
            conn.execute(
                "DELETE FROM storage_completion_tokens WHERE token = ?", (token,)
            )
        return {"object": completed}

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
        with closing(self.store.connect()) as conn:
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

    def get_object(self, *, project_id: str | None, object_id: str) -> dict[str, Any]:
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            return {
                "object": self._hydrate(
                    row=self._get_by_id(
                        conn=conn, project_id=project_id, object_id=object_id
                    )
                )
            }

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

    def _enforce_upload_size(self, *, size_bytes: int) -> None:
        if size_bytes < 0:
            raise ValidationError("size_bytes must be non-negative")
        if size_bytes > self.max_upload_bytes:
            raise ValidationError(
                f"upload is {size_bytes} bytes; the maximum is "
                f"{self.max_upload_bytes} bytes on this backend",
                details={"size_bytes": size_bytes, "max_bytes": self.max_upload_bytes},
            )
        if size_bytes > SINGLE_PUT_MAX_BYTES:
            raise ValidationError(
                f"upload is {size_bytes} bytes; single-PUT storage submission "
                f"supports up to {SINGLE_PUT_MAX_BYTES} bytes — multipart uploads "
                "for larger objects are unsupported in v1",
                details={"size_bytes": size_bytes, "max_bytes": SINGLE_PUT_MAX_BYTES},
            )

    def _mint_completion_token(
        self, *, project_id: str, object_id: str, upload_id: str
    ) -> str:
        token = secrets.token_urlsafe(24)
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO storage_completion_tokens
                  (token, project_id, object_id, upload_id, status, expires_at, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    token,
                    project_id,
                    object_id,
                    upload_id,
                    iso_after(seconds=COMPLETION_TOKEN_TTL_SECONDS),
                    now_iso(),
                ),
            )
        return token

    def _sweep_completion_tokens(self) -> None:
        """Own transaction so the sweep survives a failing completion path."""
        with self.store.transaction() as conn:
            conn.execute(
                "DELETE FROM storage_completion_tokens WHERE expires_at < ?",
                (now_iso(),),
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
    "COMPLETION_TOKEN_TTL_SECONDS",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "PRESIGN_TTL_SECONDS",
    "SINGLE_PUT_MAX_BYTES",
    "STORAGE_DEFAULT_TTL_SECONDS",
    "STORAGE_KINDS",
    "StorageLedgerService",
    "storage_fetch_command",
    "storage_submit_command",
]
