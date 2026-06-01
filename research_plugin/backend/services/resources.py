"""Repo-file resource logic."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..utils import NotFoundError, ValidationError, new_id, now_iso
from ..state.store import StateStore, row_to_dict, rows_to_dicts
from .permissions import PermissionService


def _content_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ResourceService:
    """Manages one-file-one-resource observation and associations."""

    def __init__(self, *, store: StateStore, permissions: PermissionService) -> None:
        self.store = store
        self.permissions = permissions

    def register_file(
        self,
        *,
        path: str,
        kind: str = "other",
        title: str = "",
        created_by: str = "codex",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        rel_path, file_path = self._resolve_repo_file(path=path)
        stat = file_path.stat()
        observed_at = now_iso()
        token = self._version_token(
            path=rel_path,
            mtime_ns=stat.st_mtime_ns,
            ctime_ns=stat.st_ctime_ns,
            size_bytes=stat.st_size,
        )
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            # Resource identity is (project_id, path): the same repo file can be a
            # distinct resource in different projects.
            existing = conn.execute(
                "SELECT * FROM resources WHERE project_id = ? AND path = ?",
                (project_id, rel_path),
            ).fetchone()
            if existing:
                resource_id = existing["id"]
                conn.execute(
                    """
                    UPDATE resources
                    SET kind = CASE WHEN ? = 'other' THEN kind ELSE ? END,
                        title = COALESCE(NULLIF(?, ''), title), version_token = ?,
                        mtime_ns = ?, size_bytes = ?, observed_at = ?, missing = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (kind, kind, title, token, stat.st_mtime_ns, stat.st_size, observed_at, observed_at, resource_id),
                )
                event_type = "resource.observed"
            else:
                resource_id = new_id(prefix="res")
                conn.execute(
                    """
                    INSERT INTO resources (
                      id, project_id, path, kind, title, version_token, mtime_ns,
                      size_bytes, observed_at, git_commit, missing, created_by,
                      created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        resource_id,
                        project_id,
                        rel_path,
                        kind,
                        title,
                        token,
                        stat.st_mtime_ns,
                        stat.st_size,
                        observed_at,
                        self._git_commit_or_none(path=rel_path),
                        created_by,
                        observed_at,
                        observed_at,
                    ),
                )
                event_type = "resource.registered"
            version = self._snapshot_version(
                conn=conn,
                resource_id=resource_id,
                project_id=project_id,
                rel_path=rel_path,
                file_path=file_path,
                stat=stat,
                observed_at=observed_at,
                created_by=created_by,
            )
            conn.execute(
                "UPDATE resources SET current_version_id = ? WHERE id = ?",
                (version["id"], resource_id),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type=event_type,
                target_type="resource",
                target_id=resource_id,
                payload={"path": rel_path, "version_id": version["id"]},
            )
            return self.resolve(resource_id=resource_id, conn=conn)

    def observe_file(self, *, path: str, project_id: str | None = None) -> dict[str, Any]:
        return self.register_file(path=path, project_id=project_id)

    def sync_changed_files(self, *, paths: list[str], project_id: str | None = None) -> dict[str, Any]:
        if not paths:
            raise ValidationError("paths is required for v0.0001 sync")
        resources = [self.register_file(path=path, project_id=project_id) for path in paths]
        return {"synced": resources, "count": len(resources)}

    def associate(
        self,
        *,
        resource_id: str,
        target_type: str,
        target_id: str,
        role: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        self.permissions.validate_resource_association(target_type=target_type, role=role)
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = conn.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
            if resource is None:
                raise NotFoundError(f"resource not found: {resource_id}")
            if resource["project_id"] != project_id:
                raise NotFoundError(f"resource not found in project {project_id}: {resource_id}")
            version_id = self._ensure_current_version_for_resource(conn=conn, resource=resource)
            target_project_id = self._ensure_target_exists(conn=conn, target_type=target_type, target_id=target_id)
            if target_project_id is not None and target_project_id != project_id:
                raise NotFoundError(f"{target_type} not found in project {project_id}: {target_id}")
            attempt_index = self._association_attempt_index(conn=conn, target_type=target_type, target_id=target_id)
            assoc_id = new_id(prefix="assoc")
            conn.execute(
                """
                INSERT INTO resource_associations
                  (id, resource_id, version_id, target_type, target_id, role, attempt_index, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_id, target_type, target_id, role, attempt_index)
                DO UPDATE SET version_id = excluded.version_id, created_at = excluded.created_at
                """,
                (assoc_id, resource_id, version_id, target_type, target_id, role, attempt_index, now_iso()),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="resource.associated",
                target_type=target_type,
                target_id=target_id,
                payload={"resource_id": resource_id, "version_id": version_id, "role": role, "attempt_index": attempt_index},
            )
            return self.resolve(resource_id=resource_id, conn=conn)

    def list_resources(self, *, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT * FROM resources WHERE project_id = ? ORDER BY path",
                (project_id,),
            ).fetchall()
            return {"resources": [self._hydrate_resource(row=row, conn=conn) for row in rows]}
        finally:
            conn.close()

    def resolve(
        self,
        *,
        resource_id: str,
        project_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        owns_conn = conn is None
        if conn is None:
            conn = self.store.connect()
        try:
            if owns_conn:
                project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"resource not found: {resource_id}")
            if project_id is not None and row["project_id"] != project_id:
                raise NotFoundError(f"resource not found in project {project_id}: {resource_id}")
            return self._hydrate_resource(row=row, conn=conn)
        finally:
            if owns_conn:
                conn.close()

    def history(self, *, resource_id: str, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = self.resolve(resource_id=resource_id, project_id=project_id, conn=conn)
            rows = conn.execute(
                """
                SELECT *
                FROM resource_versions
                WHERE resource_id = ? AND project_id = ?
                ORDER BY rowid
                """,
                (resource_id, project_id),
            ).fetchall()
            return {"resource": resource, "versions": [self._hydrate_version(row=row, conn=conn) for row in rows]}
        finally:
            conn.close()

    def resources_for_target(self, *, conn: sqlite3.Connection, target_type: str, target_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT r.*, a.role AS association_role, a.attempt_index AS association_attempt_index,
                   a.version_id AS association_version_id
            FROM resources r
            JOIN resource_associations a ON a.resource_id = r.id
            WHERE a.target_type = ? AND a.target_id = ?
            ORDER BY a.attempt_index, a.role, r.path
            """,
            (target_type, target_id),
        ).fetchall()
        return [
            self._hydrate_resource(row=row, conn=conn)
            | {
                "association_role": row["association_role"],
                "association_attempt_index": row["association_attempt_index"],
                "association_version_id": row["association_version_id"],
            }
            for row in rows
        ]

    def refresh_target_resources(
        self,
        *,
        conn: sqlite3.Connection,
        target_type: str,
        target_id: str,
        attempt_index: int | None = None,
    ) -> dict[str, Any]:
        query = """
            SELECT a.id AS association_id, a.version_id AS association_version_id,
                   a.role AS association_role, a.attempt_index AS association_attempt_index,
                   r.*
            FROM resource_associations a
            JOIN resources r ON r.id = a.resource_id
            WHERE a.target_type = ? AND a.target_id = ?
        """
        params: list[Any] = [target_type, target_id]
        if attempt_index is not None:
            query += " AND a.attempt_index = ?"
            params.append(attempt_index)
        changed: list[dict[str, Any]] = []
        for row in conn.execute(query, params).fetchall():
            try:
                version_id = self._ensure_current_version_for_resource(conn=conn, resource=row)
            except NotFoundError:
                conn.execute(
                    "UPDATE resources SET missing = 1, updated_at = ? WHERE id = ?",
                    (now_iso(), row["id"]),
                )
                changed.append(
                    {
                        "resource_id": row["id"],
                        "path": row["path"],
                        "role": row["association_role"],
                        "status": "missing",
                    }
                )
                continue
            if version_id != row["association_version_id"]:
                conn.execute(
                    "UPDATE resource_associations SET version_id = ? WHERE id = ?",
                    (version_id, row["association_id"]),
                )
                changed.append(
                    {
                        "resource_id": row["id"],
                        "path": row["path"],
                        "role": row["association_role"],
                        "status": "refreshed",
                        "version_id": version_id,
                    }
                )
        return {"count": len(changed), "changed": changed}

    def _hydrate_resource(self, *, row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        assoc_rows = conn.execute(
            """
            SELECT target_type, target_id, role, attempt_index, version_id
            FROM resource_associations
            WHERE resource_id = ?
            ORDER BY target_type, role, attempt_index
            """,
            (data["id"],),
        ).fetchall()
        data["associations"] = rows_to_dicts(rows=assoc_rows)
        if data.get("current_version_id"):
            row = conn.execute("SELECT * FROM resource_versions WHERE id = ?", (data["current_version_id"],)).fetchone()
            data["current_version"] = self._hydrate_version(row=row, conn=conn) if row else None
        else:
            data["current_version"] = None
        return data

    def _resolve_repo_file(self, *, path: str) -> tuple[str, Path]:
        if not path:
            raise ValidationError("path is required")
        if os.path.isabs(path):
            raise ValidationError("resource paths must be repo-relative")
        rel = Path(path)
        if any(part == ".." for part in rel.parts):
            raise ValidationError("resource path may not contain '..'")
        if rel.parts and rel.parts[0] == ".research_plugin":
            raise ValidationError("resource path may not point inside .research_plugin")
        full = (self.store.repo_root / rel).resolve()
        try:
            full.relative_to(self.store.repo_root)
        except ValueError as exc:
            raise ValidationError("resource path escapes repo root") from exc
        if not full.exists():
            raise NotFoundError(f"resource file does not exist: {path}")
        if not full.is_file():
            raise ValidationError("v0.0001 resources must be files")
        return rel.as_posix(), full

    def _ensure_target_exists(self, *, conn: sqlite3.Connection, target_type: str, target_id: str) -> str | None:
        table_by_type = {"experiment": "experiments", "claim": "claims", "review": "reviews"}
        table = table_by_type.get(target_type)
        if target_type == "attempt":
            # Attempts are implicit in v0.0001.
            return None
        if table is None:
            raise ValidationError(f"unsupported target type: {target_type}")
        row = conn.execute(f"SELECT id, project_id FROM {table} WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{target_type} not found: {target_id}")
        return str(row["project_id"])

    def _association_attempt_index(self, *, conn: sqlite3.Connection, target_type: str, target_id: str) -> int:
        if target_type != "experiment":
            return 0
        row = conn.execute("SELECT attempt_index FROM experiments WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"experiment not found: {target_id}")
        return int(row["attempt_index"])

    def _version_token(self, *, path: str, mtime_ns: int, ctime_ns: int, size_bytes: int) -> str:
        # ctime is included so an in-place edit that preserves mtime+size (e.g. a
        # restored mtime) still changes the token: content cannot change without
        # bumping the inode change time, even when mtime is held constant.
        return f"{path}:{mtime_ns}:{ctime_ns}:{size_bytes}"

    def _git_commit_or_none(self, *, path: str) -> str | None:
        # Keep this optional and failure-tolerant; resource identity is file-first.
        return None

    def _ensure_current_version_for_resource(self, *, conn: sqlite3.Connection, resource: sqlite3.Row) -> str:
        rel_path, file_path = self._resolve_repo_file(path=resource["path"])
        stat = file_path.stat()
        token = self._version_token(
            path=rel_path,
            mtime_ns=stat.st_mtime_ns,
            ctime_ns=stat.st_ctime_ns,
            size_bytes=stat.st_size,
        )
        if resource["current_version_id"] and resource["version_token"] == token:
            return str(resource["current_version_id"])
        observed_at = now_iso()
        version = self._snapshot_version(
            conn=conn,
            resource_id=resource["id"],
            project_id=resource["project_id"],
            rel_path=rel_path,
            file_path=file_path,
            stat=stat,
            observed_at=observed_at,
            created_by=resource["created_by"],
        )
        conn.execute(
            """
            UPDATE resources
            SET version_token = ?, mtime_ns = ?, size_bytes = ?, observed_at = ?,
                missing = 0, updated_at = ?, current_version_id = ?
            WHERE id = ?
            """,
            (
                token,
                stat.st_mtime_ns,
                stat.st_size,
                observed_at,
                observed_at,
                version["id"],
                resource["id"],
            ),
        )
        self.store.record_event(
            conn=conn,
            project_id=resource["project_id"],
            event_type="resource.observed",
            target_type="resource",
            target_id=resource["id"],
            payload={"path": rel_path, "version_id": version["id"]},
        )
        return str(version["id"])

    def _snapshot_version(
        self,
        *,
        conn: sqlite3.Connection,
        resource_id: str,
        project_id: str,
        rel_path: str,
        file_path: Path,
        stat: os.stat_result,
        observed_at: str,
        created_by: str,
    ) -> dict[str, Any]:
        content_sha = _content_sha256(file_path)
        content_type = mimetypes.guess_type(rel_path)[0] or "application/octet-stream"
        current = conn.execute(
            "SELECT * FROM resource_versions WHERE id = (SELECT current_version_id FROM resources WHERE id = ?)",
            (resource_id,),
        ).fetchone()
        if current and current["content_sha256"] == content_sha:
            return self._hydrate_version(row=current, conn=conn)

        version_id = new_id(prefix="rver")
        conn.execute(
            """
            INSERT INTO resource_versions (
              id, resource_id, project_id, path, content_sha256,
              size_bytes, mtime_ns, observed_at, content_type,
              created_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                resource_id,
                project_id,
                rel_path,
                content_sha,
                stat.st_size,
                stat.st_mtime_ns,
                observed_at,
                content_type,
                created_by,
                observed_at,
            ),
        )
        self.store.record_event(
            conn=conn,
            project_id=project_id,
            event_type="resource.versioned",
            target_type="resource",
            target_id=resource_id,
            payload={"path": rel_path, "version_id": version_id},
        )
        row = conn.execute("SELECT * FROM resource_versions WHERE id = ?", (version_id,)).fetchone()
        return self._hydrate_version(row=row, conn=conn)

    def _hydrate_version(self, *, row: sqlite3.Row | None, conn: sqlite3.Connection) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        if not data:
            return data
        assoc_rows = conn.execute(
            """
            SELECT target_type, target_id, role, attempt_index, created_at
            FROM resource_associations
            WHERE version_id = ?
            ORDER BY target_type, role, attempt_index
            """,
            (data["id"],),
        ).fetchall()
        data["associations"] = rows_to_dicts(rows=assoc_rows)
        return data

    def _get_version(self, *, conn: sqlite3.Connection, project_id: str, resource_id: str, version_id: str) -> dict[str, Any]:
        resource = conn.execute("SELECT id FROM resources WHERE id = ? AND project_id = ?", (resource_id, project_id)).fetchone()
        if resource is None:
            raise NotFoundError(f"resource not found in project {project_id}: {resource_id}")
        row = conn.execute(
            "SELECT * FROM resource_versions WHERE id = ? AND resource_id = ? AND project_id = ?",
            (version_id, resource_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"resource version not found: {version_id}")
        return self._hydrate_version(row=row, conn=conn)

