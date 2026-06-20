"""Repo-file resource logic."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..utils import NotFoundError, ValidationError, new_id, now_iso
from ..state.blobs import BlobStore
from ..state.store import StateStore, next_created_seq, row_to_dict, rows_to_dicts
from ..workspace import LocalWorkspace
from ..domain.vocabulary import GATED_ROLE_BYTE_CAPS
from .artifacts import markdown_image_links
from .permissions import PermissionService


def _content_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ResourceService:
    """Manages one-file-one-resource observation and associations."""

    def __init__(
        self,
        *,
        store: StateStore,
        permissions: PermissionService,
        workspace: LocalWorkspace,
        blobs: BlobStore | None = None,
    ) -> None:
        self.store = store
        self.permissions = permissions
        # File observation reads the working tree through the workspace, not
        # the record store; the split-mode reshape (plan Phase 8) moves these
        # reads behind the worker's observe_file/read_artifact_bytes duties
        # (plan §3.1).
        self.workspace = workspace
        self.blobs = blobs

    def register_file(
        self,
        *,
        path: str | None = None,
        paths: list[str] | None = None,
        kind: str = "other",
        title: str = "",
        created_by: str = "codex",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Register/observe one file (``path``) or a batch (``paths``).

        A single ``path`` returns the resolved resource. A ``paths`` batch
        returns ``{"synced": [...], "count": n}`` so one tool covers both the
        single-file and changed-files-sweep cases.
        """
        if paths:
            resources = [
                self._register_one(
                    path=p, kind=kind, title=title, created_by=created_by, project_id=project_id
                )
                for p in paths
            ]
            return {"synced": resources, "count": len(resources)}
        if not path:
            raise ValidationError(
                "resource.register_file requires 'path' (a single file) or 'paths' (a batch)"
            )
        return self._register_one(
            path=path, kind=kind, title=title, created_by=created_by, project_id=project_id
        )

    def _register_one(
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
        return self.record_observation(
            path=rel_path,
            kind=kind,
            title=title,
            created_by=created_by,
            project_id=project_id,
            mtime_ns=stat.st_mtime_ns,
            ctime_ns=stat.st_ctime_ns,
            size_bytes=stat.st_size,
            content_sha256=_content_sha256(file_path),
            content_type=mimetypes.guess_type(rel_path)[0] or "application/octet-stream",
        )

    def record_observation(
        self,
        *,
        path: str,
        kind: str = "other",
        title: str = "",
        created_by: str = "codex",
        project_id: str | None = None,
        mtime_ns: int,
        ctime_ns: int,
        size_bytes: int,
        content_sha256: str,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Record a file observation supplied by the local data plane.

        The control plane stores only repo-relative resource identity,
        version metadata, and content hashes; the daemon owns local path
        resolution and file reads.
        """
        rel_path = self._repo_relative_path(path=path)
        self._validate_content_sha256(content_sha256)
        observed_at = now_iso()
        token = self._version_token(
            path=rel_path,
            mtime_ns=int(mtime_ns),
            ctime_ns=int(ctime_ns),
            size_bytes=int(size_bytes),
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
                        deleted = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (kind, kind, title, token, int(mtime_ns), int(size_bytes), observed_at, observed_at, resource_id),
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
                        int(mtime_ns),
                        int(size_bytes),
                        observed_at,
                        self._git_commit_or_none(path=rel_path),
                        created_by,
                        observed_at,
                        observed_at,
                    ),
                )
                event_type = "resource.registered"
            version = self._snapshot_version_record(
                conn=conn,
                resource_id=resource_id,
                project_id=project_id,
                rel_path=rel_path,
                content_sha256=content_sha256,
                size_bytes=int(size_bytes),
                mtime_ns=int(mtime_ns),
                content_type=content_type or "application/octet-stream",
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

    def delete(self, *, resource_id: str, project_id: str | None = None) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = conn.execute(
                "SELECT * FROM resources WHERE id = ? AND project_id = ?",
                (resource_id, project_id),
            ).fetchone()
            if resource is None:
                raise NotFoundError(f"resource not found in project {project_id}: {resource_id}")
            if int(resource["deleted"] or 0):
                return {"deleted": False, "resource": self._hydrate_resource(row=resource, conn=conn)}
            deleted_at = now_iso()
            association_count = conn.execute(
                "SELECT COUNT(*) AS count FROM resource_associations WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()["count"]
            conn.execute("DELETE FROM resource_associations WHERE resource_id = ?", (resource_id,))
            conn.execute(
                """
                UPDATE resources
                SET deleted = 1, missing = 1, updated_at = ?
                WHERE id = ?
                """,
                (deleted_at, resource_id),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="resource.deleted",
                target_type="resource",
                target_id=resource_id,
                payload={"path": resource["path"], "removed_associations": association_count},
            )
            deleted = conn.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
            return {
                "deleted": True,
                "removed_associations": association_count,
                "resource": self._hydrate_resource(row=deleted, conn=conn),
            }

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
        storage_target_type = self.permissions.storage_resource_target_type(
            target_type=target_type
        )
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = conn.execute("SELECT * FROM resources WHERE id = ? AND deleted = 0", (resource_id,)).fetchone()
            if resource is None:
                raise NotFoundError(f"resource not found: {resource_id}")
            if resource["project_id"] != project_id:
                raise NotFoundError(f"resource not found in project {project_id}: {resource_id}")
            version_id = self._ensure_current_version_for_resource(conn=conn, resource=resource)
            self._capture_gated_blob(
                conn=conn,
                resource=resource,
                role=role,
                version_id=version_id,
                project_id=project_id,
            )
            return self._associate_version(
                conn=conn,
                project_id=project_id,
                resource_id=resource_id,
                version_id=version_id,
                storage_target_type=storage_target_type,
                public_target_type=target_type,
                target_id=target_id,
                role=role,
            )

    def associate_observed(
        self,
        *,
        resource_id: str,
        target_type: str,
        target_id: str,
        role: str,
        project_id: str | None = None,
        content_bytes: bytes | None = None,
        figures: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Associate the current observed version, with daemon-submitted bytes.

        Hosted control cannot read the working tree. For gated roles the
        daemon submits the artifact bytes it just read locally; control checks
        them against the pinned version hash before storing blobs.
        """
        self.permissions.validate_resource_association(target_type=target_type, role=role)
        storage_target_type = self.permissions.storage_resource_target_type(
            target_type=target_type
        )
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = conn.execute("SELECT * FROM resources WHERE id = ? AND deleted = 0", (resource_id,)).fetchone()
            if resource is None:
                raise NotFoundError(f"resource not found: {resource_id}")
            if resource["project_id"] != project_id:
                raise NotFoundError(f"resource not found in project {project_id}: {resource_id}")
            version_id = str(resource["current_version_id"] or "")
            if not version_id:
                raise ValidationError(
                    "resource must be observed before it can be associated",
                    details={"resource_id": resource_id},
                )
            self._capture_submitted_gated_blob(
                conn=conn,
                resource=resource,
                role=role,
                version_id=version_id,
                project_id=project_id,
                content_bytes=content_bytes,
                figures=figures or [],
            )
            return self._associate_version(
                conn=conn,
                project_id=project_id,
                resource_id=resource_id,
                version_id=version_id,
                storage_target_type=storage_target_type,
                public_target_type=target_type,
                target_id=target_id,
                role=role,
            )

    def validate_association_intent(
        self,
        *,
        resource_id: str,
        target_type: str,
        target_id: str,
        role: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        self.permissions.validate_resource_association(target_type=target_type, role=role)
        storage_target_type = self.permissions.storage_resource_target_type(
            target_type=target_type
        )
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = conn.execute(
                "SELECT * FROM resources WHERE id = ? AND deleted = 0", (resource_id,)
            ).fetchone()
            if resource is None:
                raise NotFoundError(f"resource not found: {resource_id}")
            if resource["project_id"] != project_id:
                raise NotFoundError(f"resource not found in project {project_id}: {resource_id}")
            target_project_id = self._ensure_target_exists(
                conn=conn,
                target_type=storage_target_type,
                target_id=target_id,
            )
            if target_project_id is not None and target_project_id != project_id:
                raise NotFoundError(f"{target_type} not found in project {project_id}: {target_id}")
            attempt_index = self._association_attempt_index(
                conn=conn,
                target_type=storage_target_type,
                target_id=target_id,
            )
            return {
                "ok": True,
                "resource": self._hydrate_resource(row=resource, conn=conn),
                "storage_target_type": storage_target_type,
                "attempt_index": attempt_index,
            }

    def list_resources(
        self,
        *,
        project_id: str | None = None,
        kind: str | None = None,
        experiment_id: str | None = None,
        missing: bool | None = None,
        compact: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List registered resources with optional filters + pagination.

        ``compact=True`` returns a lean projection without the heavy nested
        ``current_version`` so large projects can be listed (and change-detected
        via ``version_token``) without re-pulling hundreds of KB per call.
        """
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            where = ["r.project_id = ?", "r.deleted = 0"]
            params: list[Any] = [project_id]
            if kind:
                where.append("r.kind = ?")
                params.append(kind)
            if missing is not None:
                where.append("r.missing = ?")
                params.append(1 if missing else 0)
            join = ""
            if experiment_id:
                join = (
                    " JOIN resource_associations a ON a.resource_id = r.id "
                    "AND a.target_type = 'experiment' AND a.target_id = ?"
                )
                params.insert(0, experiment_id)  # join param precedes WHERE params
            base = f"FROM resources r{join} WHERE {' AND '.join(where)}"
            total = int(
                conn.execute(f"SELECT count(DISTINCT r.id) {base}", params).fetchone()[0]
            )
            query = f"SELECT DISTINCT r.* {base} ORDER BY r.path"
            page_params = list(params)
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                page_params += [int(limit), int(offset)]
            elif offset:
                query += " LIMIT -1 OFFSET ?"
                page_params.append(int(offset))
            rows = conn.execute(query, page_params).fetchall()
            resources = [
                self._hydrate_resource(row=row, conn=conn, compact=compact) for row in rows
            ]
            returned = len(resources)
            return {
                "resources": resources,
                "count": returned,
                "returned": returned,
                "total": total,
                "offset": int(offset),
                "has_more": (int(offset) + returned) < total,
                "compact": bool(compact),
            }
        finally:
            conn.close()

    def resolve(
        self,
        *,
        resource_id: str,
        project_id: str | None = None,
        include_history: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Resolve one registered resource.

        ``include_history=True`` attaches the resource's immutable observed
        ``versions`` (oldest-first) under a ``versions`` key — folding what was
        the separate ``resource.history`` tool into this one.
        """
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
            resource = self._hydrate_resource(row=row, conn=conn)
            if include_history:
                version_rows = conn.execute(
                    """
                    SELECT *
                    FROM resource_versions
                    WHERE resource_id = ? AND project_id = ?
                    ORDER BY created_seq
                    """,
                    (resource_id, row["project_id"]),
                ).fetchall()
                resource["versions"] = [
                    self._hydrate_version(row=version_row, conn=conn) for version_row in version_rows
                ]
            return resource
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
                ORDER BY created_seq
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
              AND r.deleted = 0
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

    def backfill_gated_blobs(self) -> int:
        """One-time local upgrade: capture bytes for gated associations made
        before byte capture existed (cloud plan Phase 2). Only versions whose
        working-tree file still matches the pinned content_sha256 can be
        recovered; anything else surfaces later as re-associate guidance from
        the gates. Idempotent — blobs already present are skipped."""
        if self.blobs is None:
            return 0
        backfilled = 0
        with self.store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT v.id AS version_id, v.project_id, v.path,
                       v.content_sha256, v.content_type, a.role
                FROM resource_associations a
                JOIN resource_versions v ON v.id = a.version_id
                WHERE a.role IN ({placeholders})
                """.format(
                    placeholders=",".join("?" * len(GATED_ROLE_BYTE_CAPS))
                ),
                tuple(sorted(GATED_ROLE_BYTE_CAPS)),
            ).fetchall()
            for row in rows:
                namespace = str(row["project_id"])
                sha = str(row["content_sha256"])
                if self.blobs.stat(namespace=namespace, sha256=sha) is not None:
                    continue
                full = self.workspace.repo_root / str(row["path"])
                try:
                    data = full.read_bytes()
                except OSError:
                    continue
                if hashlib.sha256(data).hexdigest() != sha:
                    continue
                self.blobs.put(
                    namespace=namespace,
                    data=data,
                    content_type=str(row["content_type"]),
                )
                backfilled += 1
                if str(row["role"]) in {"report", "reflection_doc", "synthesis_doc"}:
                    try:
                        self._capture_markdown_figures(
                            conn=conn,
                            version_id=str(row["version_id"]),
                            project_id=namespace,
                            markdown_rel_path=str(row["path"]),
                            markdown_text=data.decode("utf-8", errors="replace"),
                            label=str(row["role"]),
                        )
                    except ValidationError:
                        pass  # bad figure links surface via the lint, not the backfill
        return backfilled

    _COMPACT_FIELDS = (
        "id", "project_id", "path", "kind", "title", "current_version_id",
        "version_token", "missing", "updated_at",
    )

    def _hydrate_resource(
        self, *, row: sqlite3.Row, conn: sqlite3.Connection, compact: bool = False
    ) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        if compact:
            # Lean projection: omit associations + the heavy nested current_version.
            # version_token is kept so callers can detect changes cheaply.
            return {k: data.get(k) for k in self._COMPACT_FIELDS}
        assoc_rows = conn.execute(
            """
            SELECT target_type, target_id, role, attempt_index, version_id
            FROM resource_associations
            WHERE resource_id = ?
            ORDER BY target_type, role, attempt_index
            """,
            (data["id"],),
        ).fetchall()
        associations = rows_to_dicts(rows=assoc_rows)
        for assoc in associations:
            if assoc.get("target_type") == "synthesis":
                assoc["target_type"] = "reflection"
        data["associations"] = associations
        if data.get("current_version_id"):
            row = conn.execute("SELECT * FROM resource_versions WHERE id = ?", (data["current_version_id"],)).fetchone()
            data["current_version"] = self._hydrate_version(row=row, conn=conn) if row else None
        else:
            data["current_version"] = None
        return data

    def _repo_relative_path(self, *, path: str) -> str:
        if not path:
            raise ValidationError("path is required")
        if os.path.isabs(path):
            raise ValidationError("resource paths must be repo-relative")
        rel = Path(path)
        if any(part == ".." for part in rel.parts):
            raise ValidationError("resource path may not contain '..'")
        if rel.parts and rel.parts[0] == ".research_plugin":
            raise ValidationError("resource path may not point inside .research_plugin")
        return rel.as_posix()

    def _resolve_repo_file(self, *, path: str) -> tuple[str, Path]:
        rel_path = self._repo_relative_path(path=path)
        rel = Path(rel_path)
        full = (self.workspace.repo_root / rel).resolve()
        try:
            full.relative_to(self.workspace.repo_root)
        except ValueError as exc:
            raise ValidationError("resource path escapes repo root") from exc
        if not full.exists():
            raise NotFoundError(f"resource file does not exist: {path}")
        if not full.is_file():
            raise ValidationError("v0.0001 resources must be files")
        return rel.as_posix(), full

    def _validate_content_sha256(self, value: str) -> None:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValidationError("content_sha256 must be a lowercase sha256 hex digest")

    def _validate_submitted_figure_link(self, *, link: str) -> None:
        if not link:
            raise ValidationError("figure link is required")
        if os.path.isabs(link):
            raise ValidationError("figure links must be repo-relative")
        if any(part == ".." for part in Path(link).parts):
            raise ValidationError("figure links may not contain '..'")

    def _ensure_target_exists(self, *, conn: sqlite3.Connection, target_type: str, target_id: str) -> str | None:
        table_by_type = {
            "experiment": "experiments",
            "synthesis": "syntheses",
            "claim": "claims",
            "review": "reviews",
        }
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
        # Experiments and syntheses both scope associations to their current
        # attempt, so a review rejection that bumps the attempt naturally
        # invalidates stale associations for either target kind.
        table_by_type = {"experiment": "experiments", "synthesis": "syntheses"}
        table = table_by_type.get(target_type)
        if table is None:
            return 0
        row = conn.execute(f"SELECT attempt_index FROM {table} WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{target_type} not found: {target_id}")
        return int(row["attempt_index"])

    def _capture_gated_blob(
        self,
        *,
        conn: sqlite3.Connection,
        resource: sqlite3.Row,
        role: str,
        version_id: str,
        project_id: str,
    ) -> None:
        """Snapshot a gated-role file's bytes into the blob store at associate.

        Decision 6 of the cloud migration plan: gated artifacts (plan, report,
        graph, project_graph, reflection_doc (legacy synthesis_doc),
        reflection_lens_doc (legacy reflection), change_spec; legacy proposals)
        submit their content when associated, so gates can lint immutable pinned
        bytes instead of live files. Size caps are
        enforced here — before any bytes are stored — with the version's
        content_sha256 as the blob key, so blob and pin cannot disagree.
        """
        cap = GATED_ROLE_BYTE_CAPS.get(role)
        if cap is None or self.blobs is None:
            return
        rel_path, file_path = self._resolve_repo_file(path=resource["path"])
        size = file_path.stat().st_size
        if size > cap:
            raise ValidationError(
                f"{rel_path} is {size} bytes; the maximum for a role-{role!r} "
                f"artifact is {cap} bytes — slim the file before associating "
                "(move raw data/outputs elsewhere and reference them)",
                details={"path": rel_path, "role": role, "size_bytes": size, "max_bytes": cap},
            )
        data = file_path.read_bytes()
        version = conn.execute(
            "SELECT content_sha256, content_type FROM resource_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if version is None:
            raise NotFoundError(f"resource version not found: {version_id}")
        sha = hashlib.sha256(data).hexdigest()
        if sha != str(version["content_sha256"]):
            raise ValidationError(
                f"{rel_path} changed while associating — retry the call",
                details={"path": rel_path, "role": role},
            )
        self.blobs.put(
            namespace=project_id,
            data=data,
            content_type=str(version["content_type"]),
        )
        if role in {"report", "reflection_doc", "synthesis_doc"}:
            self._capture_markdown_figures(
                conn=conn,
                version_id=version_id,
                project_id=project_id,
                markdown_rel_path=rel_path,
                markdown_text=data.decode("utf-8", errors="replace"),
                label=role,
            )

    def _capture_submitted_gated_blob(
        self,
        *,
        conn: sqlite3.Connection,
        resource: sqlite3.Row,
        role: str,
        version_id: str,
        project_id: str,
        content_bytes: bytes | None,
        figures: list[dict[str, Any]],
    ) -> None:
        cap = GATED_ROLE_BYTE_CAPS.get(role)
        if cap is None or self.blobs is None:
            return
        if content_bytes is None:
            raise ValidationError(
                f"role-{role!r} associations require artifact bytes from the data plane",
                details={"resource_id": resource["id"], "role": role},
            )
        size = len(content_bytes)
        if size > cap:
            raise ValidationError(
                f"{resource['path']} is {size} bytes; the maximum for a role-{role!r} "
                f"artifact is {cap} bytes — slim the file before associating "
                "(move raw data/outputs elsewhere and reference them)",
                details={"path": resource["path"], "role": role, "size_bytes": size, "max_bytes": cap},
            )
        version = conn.execute(
            "SELECT content_sha256, content_type FROM resource_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if version is None:
            raise NotFoundError(f"resource version not found: {version_id}")
        sha = hashlib.sha256(content_bytes).hexdigest()
        if sha != str(version["content_sha256"]):
            raise ValidationError(
                f"{resource['path']} changed while associating — retry the call",
                details={"path": resource["path"], "role": role},
            )
        self.blobs.put(
            namespace=project_id,
            data=content_bytes,
            content_type=str(version["content_type"]),
        )
        if role in {"report", "reflection_doc", "synthesis_doc"}:
            self._capture_submitted_markdown_figures(
                conn=conn,
                version_id=version_id,
                project_id=project_id,
                markdown_text=content_bytes.decode("utf-8", errors="replace"),
                figures=figures,
            )

    # Figures referenced by markdown gated artifacts can be real images; cap
    # each upload.
    FIGURE_MAX_BYTES = 5_000_000

    def _capture_markdown_figures(
        self,
        *,
        conn: sqlite3.Connection,
        version_id: str,
        project_id: str,
        markdown_rel_path: str,
        markdown_text: str,
        label: str,
    ) -> None:
        """Submit a markdown artifact's relative figure links alongside it.

        Each resolvable, repo-jailed, size-capped figure file is captured as a
        blob and recorded in report_figures keyed by the artifact's version —
        the mapping markdown lints and the UI use. A link whose file is
        missing or oversize is simply not recorded; the lint names it with
        re-associate guidance. A link escaping the repo is rejected outright."""
        markdown_dir = (self.workspace.repo_root / markdown_rel_path).parent
        for link in markdown_image_links(markdown_text):
            resolved = (markdown_dir / link).resolve()
            try:
                resolved.relative_to(self.workspace.repo_root)
            except ValueError as exc:
                raise ValidationError(
                    f"{label} image link escapes the repo: {link}",
                    details={"link": link, "resource": markdown_rel_path, "role": label},
                ) from exc
            if not resolved.is_file():
                continue
            size = resolved.stat().st_size
            if size > self.FIGURE_MAX_BYTES:
                continue
            figure_data = resolved.read_bytes()
            sha = self.blobs.put(
                namespace=project_id,
                data=figure_data,
                content_type=mimetypes.guess_type(link)[0] or "application/octet-stream",
            )
            conn.execute(
                """
                INSERT INTO report_figures (report_version_id, link_path, sha256, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(report_version_id, link_path)
                DO UPDATE SET sha256 = excluded.sha256, size_bytes = excluded.size_bytes,
                              created_at = excluded.created_at
                """,
                (version_id, link, sha, size, now_iso()),
            )

    def _capture_submitted_markdown_figures(
        self,
        *,
        conn: sqlite3.Connection,
        version_id: str,
        project_id: str,
        markdown_text: str,
        figures: list[dict[str, Any]],
    ) -> None:
        submitted = {str(figure.get("link_path") or ""): figure for figure in figures}
        for link in markdown_image_links(markdown_text):
            figure = submitted.get(link)
            if figure is None:
                continue
            self._validate_submitted_figure_link(link=link)
            data = figure.get("data")
            if not isinstance(data, bytes):
                continue
            size = len(data)
            if size > self.FIGURE_MAX_BYTES:
                continue
            sha = self.blobs.put(
                namespace=project_id,
                data=data,
                content_type=str(figure.get("content_type") or "application/octet-stream"),
            )
            conn.execute(
                """
                INSERT INTO report_figures (report_version_id, link_path, sha256, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(report_version_id, link_path)
                DO UPDATE SET sha256 = excluded.sha256, size_bytes = excluded.size_bytes,
                              created_at = excluded.created_at
                """,
                (version_id, link, sha, size, now_iso()),
            )

    def _associate_version(
        self,
        *,
        conn: sqlite3.Connection,
        project_id: str,
        resource_id: str,
        version_id: str,
        storage_target_type: str,
        public_target_type: str,
        target_id: str,
        role: str,
    ) -> dict[str, Any]:
        target_project_id = self._ensure_target_exists(
            conn=conn,
            target_type=storage_target_type,
            target_id=target_id,
        )
        if target_project_id is not None and target_project_id != project_id:
            raise NotFoundError(f"{public_target_type} not found in project {project_id}: {target_id}")
        attempt_index = self._association_attempt_index(
            conn=conn,
            target_type=storage_target_type,
            target_id=target_id,
        )
        assoc_id = new_id(prefix="assoc")
        conn.execute(
            """
            INSERT INTO resource_associations
              (id, resource_id, version_id, target_type, target_id, role, attempt_index, created_at, created_seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_id, target_type, target_id, role, attempt_index)
            DO UPDATE SET version_id = excluded.version_id, created_at = excluded.created_at
            """,
            (
                assoc_id,
                resource_id,
                version_id,
                storage_target_type,
                target_id,
                role,
                attempt_index,
                now_iso(),
                # An upsert keeps the original created_seq (rowid parity).
                next_created_seq(conn=conn, table="resource_associations"),
            ),
        )
        self.store.record_event(
            conn=conn,
            project_id=project_id,
            event_type="resource.associated",
            target_type=storage_target_type,
            target_id=target_id,
            payload={"resource_id": resource_id, "version_id": version_id, "role": role, "attempt_index": attempt_index},
        )
        return self.resolve(resource_id=resource_id, conn=conn)

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
        return self._snapshot_version_record(
            conn=conn,
            resource_id=resource_id,
            project_id=project_id,
            rel_path=rel_path,
            content_sha256=_content_sha256(file_path),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            content_type=mimetypes.guess_type(rel_path)[0] or "application/octet-stream",
            observed_at=observed_at,
            created_by=created_by,
        )

    def _snapshot_version_record(
        self,
        *,
        conn: sqlite3.Connection,
        resource_id: str,
        project_id: str,
        rel_path: str,
        content_sha256: str,
        size_bytes: int,
        mtime_ns: int,
        content_type: str,
        observed_at: str,
        created_by: str,
    ) -> dict[str, Any]:
        self._validate_content_sha256(content_sha256)
        current = conn.execute(
            "SELECT * FROM resource_versions WHERE id = (SELECT current_version_id FROM resources WHERE id = ?)",
            (resource_id,),
        ).fetchone()
        if current and current["content_sha256"] == content_sha256:
            return self._hydrate_version(row=current, conn=conn)

        version_id = new_id(prefix="rver")
        conn.execute(
            """
            INSERT INTO resource_versions (
              id, resource_id, project_id, path, content_sha256,
              size_bytes, mtime_ns, observed_at, content_type,
              created_by, created_at, created_seq
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                resource_id,
                project_id,
                rel_path,
                content_sha256,
                int(size_bytes),
                int(mtime_ns),
                observed_at,
                content_type or "application/octet-stream",
                created_by,
                observed_at,
                next_created_seq(conn=conn, table="resource_versions"),
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
        associations = rows_to_dicts(rows=assoc_rows)
        for assoc in associations:
            if assoc.get("target_type") == "synthesis":
                assoc["target_type"] = "reflection"
        data["associations"] = associations
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
