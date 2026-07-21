"""Repo-file resource logic."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from merv.shared.artifact_roles import (
    GATED_ROLE_BYTE_CAPS,
    SYSTEM_CREATED_BY,
    metric_result_capture_cap,
)
from merv.shared.markdown_images import (
    MARKDOWN_FIGURE_MAX_BYTES,
    MARKDOWN_FIGURE_ROLES,
    markdown_image_links,
)
from merv.shared.project_dirs import PROJECT_STATE_DIR_NAMES

from ..kernel.utils import (
    NotFoundError,
    ValidationError,
    WorkflowError,
    new_id,
    now_iso,
)
from ..kernel.ports.blob_store import EvidenceBlobStore
from ..kernel.state.store import (
    BaseStateStore,
    Connection,
    Row,
    next_created_seq,
    row_to_dict,
    rows_to_dicts,
)
from .pinned import pinned_text_for_version as load_pinned_text_for_version
from .association_policy import validate_resource_association


class ResourceService:
    """Manages one-file-one-resource observation and associations."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        blobs: EvidenceBlobStore | None = None,
        association_targets: Any = None,
    ) -> None:
        self.store = store
        self.blobs = blobs
        # Research-core-owned target resolution (existence + attempt scoping),
        # injected at composition — artifacts must not name research-core
        # tables. Optional only for direct construction in tests; the
        # composition root always injects it.
        self.association_targets = association_targets

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
        version metadata, and content hashes; the MCP proxy owns local path
        resolution and file reads.
        """
        rel_path = self._repo_relative_path(path=path)
        self._validate_content_sha256(content_sha256)
        observed_at = now_iso()
        # ctime is included so an in-place edit that preserves mtime+size (e.g. a
        # restored mtime) still changes the token: content cannot change without
        # bumping the inode change time, even when mtime is held constant.
        token = f"{rel_path}:{int(mtime_ns)}:{int(ctime_ns)}:{int(size_bytes)}"
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            # Resource identity is (project_id, path): the same repo file can be a
            # distinct resource in different projects.
            existing = conn.execute(
                "SELECT * FROM resources WHERE project_id = ? AND path = ?",
                (project_id, rel_path),
            ).fetchone()
            if existing and str(existing["created_by"]) == SYSTEM_CREATED_BY:
                raise ValidationError(
                    f"{rel_path} is a system-generated artifact; it cannot be "
                    "re-registered or replaced"
                )
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
                    (
                        kind,
                        kind,
                        title,
                        token,
                        int(mtime_ns),
                        int(size_bytes),
                        observed_at,
                        observed_at,
                        resource_id,
                    ),
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
                        None,  # Optional git provenance; resource identity is file-first.
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

    def delete(
        self, *, resource_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = conn.execute(
                "SELECT * FROM resources WHERE id = ? AND project_id = ?",
                (resource_id, project_id),
            ).fetchone()
            if resource is None:
                raise NotFoundError(
                    f"resource not found in project {project_id}: {resource_id}"
                )
            if str(resource["created_by"]) == SYSTEM_CREATED_BY:
                raise ValidationError(
                    f"{resource['path']} is a system-generated artifact; it "
                    "cannot be deleted"
                )
            if int(resource["deleted"] or 0):
                return {
                    "deleted": False,
                    "resource": self._hydrate_resource(row=resource, conn=conn),
                }
            deleted_at = now_iso()
            association_count = conn.execute(
                "SELECT COUNT(*) AS count FROM resource_associations WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()["count"]
            conn.execute(
                "DELETE FROM resource_associations WHERE resource_id = ?",
                (resource_id,),
            )
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
                payload={
                    "path": resource["path"],
                    "removed_associations": association_count,
                },
            )
            deleted = conn.execute(
                "SELECT * FROM resources WHERE id = ?", (resource_id,)
            ).fetchone()
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
        """Record an already-observed association.

        Local and daemon tool paths call ``associate_observed`` with bytes for
        gated roles after the data plane has read the working tree. Keeping this
        fallback record-only prevents the service from reaching into local files.
        """
        return self.associate_observed(
            resource_id=resource_id,
            target_type=target_type,
            target_id=target_id,
            role=role,
            project_id=project_id,
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
        """Associate the current observed version, with proxy-submitted bytes.

        Hosted control cannot read the working tree. For gated roles the
        MCP proxy submits the artifact bytes it just read locally; control
        checks them against the pinned version hash before storing blobs.
        """
        validate_resource_association(target_type=target_type, role=role)
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = conn.execute(
                "SELECT * FROM resources WHERE id = ? AND deleted = 0", (resource_id,)
            ).fetchone()
            if resource is None:
                raise NotFoundError(f"resource not found: {resource_id}")
            if resource["project_id"] != project_id:
                raise NotFoundError(
                    f"resource not found in project {project_id}: {resource_id}"
                )
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
                target_type=target_type,
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
        validate_resource_association(target_type=target_type, role=role)
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = conn.execute(
                "SELECT * FROM resources WHERE id = ? AND deleted = 0", (resource_id,)
            ).fetchone()
            if resource is None:
                raise NotFoundError(f"resource not found: {resource_id}")
            if resource["project_id"] != project_id:
                raise NotFoundError(
                    f"resource not found in project {project_id}: {resource_id}"
                )
            target_project_id = self._targets().project_id_for(
                conn=conn,
                target_type=target_type,
                target_id=target_id,
            )
            if target_project_id is not None and target_project_id != project_id:
                raise NotFoundError(
                    f"{target_type} not found in project {project_id}: {target_id}"
                )
            attempt_index = self._targets().attempt_index_for(
                conn=conn,
                target_type=target_type,
                target_id=target_id,
            )
            return {
                "ok": True,
                "resource": self._hydrate_resource(row=resource, conn=conn),
                "target_type": target_type,
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
        with closing(self.store.connect()) as conn:
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
            total_row = conn.execute(
                f"SELECT count(DISTINCT r.id) AS total {base}", params
            ).fetchone()
            total = int(total_row["total"] if total_row is not None else 0)
            query = f"SELECT DISTINCT r.* {base} ORDER BY r.path"
            page_params = list(params)
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                page_params += [int(limit), int(offset)]
            elif offset:
                query += " LIMIT ? OFFSET ?"
                page_params.append(2_147_483_647)
                page_params.append(int(offset))
            rows = conn.execute(query, page_params).fetchall()
            resources = [
                self._hydrate_resource(row=row, conn=conn, compact=compact)
                for row in rows
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

    def resolve(
        self,
        *,
        resource_id: str,
        project_id: str | None = None,
        include_history: bool = False,
        conn: Connection | None = None,
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
                project_id = self.store.require_project_id(
                    conn=conn, project_id=project_id
                )
            row = conn.execute(
                "SELECT * FROM resources WHERE id = ?", (resource_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"resource not found: {resource_id}")
            if project_id is not None and row["project_id"] != project_id:
                raise NotFoundError(
                    f"resource not found in project {project_id}: {resource_id}"
                )
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
                    self._hydrate_version(row=version_row, conn=conn)
                    for version_row in version_rows
                ]
            return resource
        finally:
            if owns_conn:
                conn.close()

    def history(
        self, *, resource_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            resource = self.resolve(
                resource_id=resource_id, project_id=project_id, conn=conn
            )
            rows = conn.execute(
                """
                SELECT *
                FROM resource_versions
                WHERE resource_id = ? AND project_id = ?
                ORDER BY created_seq
                """,
                (resource_id, project_id),
            ).fetchall()
            return {
                "resource": resource,
                "versions": [self._hydrate_version(row=row, conn=conn) for row in rows],
            }

    def pinned_text_for_version(
        self, *, version_id: str, what: str, role: str = ""
    ) -> str:
        """Submitted UTF-8 text for one resource version, or WorkflowError.

        This is the strict reader used by explicit versioned presentation
        paths. The looser ``submitted_text_for_version`` below powers UI
        fallbacks that should degrade to unavailable instead of raising.
        """
        if self.blobs is None:
            raise WorkflowError(
                f"{what}: no blob store is configured; submitted content is unavailable"
            )
        with closing(self.store.connect()) as conn:
            return load_pinned_text_for_version(
                conn=conn,
                blobs=self.blobs,
                version_id=version_id,
                what=what,
                role=role,
            )

    def submitted_text_for_version(self, *, version_id: str | None) -> str | None:
        """Best-effort submitted text for one version, decoded for UI display."""
        if not version_id or self.blobs is None:
            return None
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                "SELECT project_id, content_sha256 FROM resource_versions WHERE id = ?",
                (str(version_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            data = self.blobs.get(
                namespace=str(row["project_id"]), sha256=str(row["content_sha256"])
            )
        except NotFoundError:
            return None
        return data.decode("utf-8", errors="replace")

    def submitted_figure(
        self, *, version_id: str | None, link_path: str
    ) -> bytes | None:
        """Best-effort submitted figure bytes for a markdown image link."""
        if not version_id or self.blobs is None:
            return None
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                """
                SELECT v.project_id, f.sha256
                FROM report_figures f
                JOIN resource_versions v ON v.id = f.report_version_id
                WHERE f.report_version_id = ? AND f.link_path = ?
                """,
                (str(version_id), link_path),
            ).fetchone()
        if row is None:
            return None
        try:
            return self.blobs.get(
                namespace=str(row["project_id"]), sha256=str(row["sha256"])
            )
        except NotFoundError:
            return None

    def resources_for_target(
        self, *, conn: Connection, target_type: str, target_id: str
    ) -> list[dict[str, Any]]:
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

    _COMPACT_FIELDS = (
        "id",
        "project_id",
        "path",
        "kind",
        "title",
        "current_version_id",
        "version_token",
        "missing",
        "updated_at",
    )

    def _hydrate_resource(
        self, *, row: Row, conn: Connection, compact: bool = False
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
        data["associations"] = rows_to_dicts(rows=assoc_rows)
        if data.get("current_version_id"):
            row = conn.execute(
                "SELECT * FROM resource_versions WHERE id = ?",
                (data["current_version_id"],),
            ).fetchone()
            data["current_version"] = (
                self._hydrate_version(row=row, conn=conn) if row else None
            )
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
        if rel.parts and rel.parts[0] in PROJECT_STATE_DIR_NAMES:
            raise ValidationError(
                "resource path may not point inside the project state dir "
                "(.merv or .research_plugin)"
            )
        return rel.as_posix()

    def _validate_content_sha256(self, value: str) -> None:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValidationError(
                "content_sha256 must be a lowercase sha256 hex digest"
            )

    def _targets(self) -> Any:
        if self.association_targets is None:
            raise RuntimeError(
                "ResourceService needs association_targets injected at composition"
            )
        return self.association_targets

    def _capture_submitted_gated_blob(
        self,
        *,
        conn: Connection,
        resource: Row,
        role: str,
        version_id: str,
        project_id: str,
        content_bytes: bytes | None,
        figures: list[dict[str, Any]],
    ) -> None:
        cap = GATED_ROLE_BYTE_CAPS.get(role)
        if self.blobs is None:
            return
        if cap is None:
            self._capture_metric_result_bytes(
                conn=conn,
                resource=resource,
                role=role,
                version_id=version_id,
                project_id=project_id,
                content_bytes=content_bytes,
            )
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
                details={
                    "path": resource["path"],
                    "role": role,
                    "size_bytes": size,
                    "max_bytes": cap,
                },
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
        if role in MARKDOWN_FIGURE_ROLES:
            self._capture_submitted_markdown_figures(
                conn=conn,
                version_id=version_id,
                project_id=project_id,
                markdown_text=content_bytes.decode("utf-8", errors="replace"),
                figures=figures,
            )

    def _capture_metric_result_bytes(
        self,
        *,
        conn: Connection,
        resource: Row,
        role: str,
        version_id: str,
        project_id: str,
        content_bytes: bytes | None,
    ) -> None:
        """Pin small JSON metric files at role-'result' associate so the
        metrics exhibit can ingest their numbers. Opportunistic by contract:
        no bytes (older proxy, over-cap file) associates exactly as before."""
        cap = metric_result_capture_cap(role=role, path=str(resource["path"]))
        if cap is None or content_bytes is None or len(content_bytes) > cap:
            return
        version = conn.execute(
            "SELECT content_sha256, content_type FROM resource_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if version is None:
            raise NotFoundError(f"resource version not found: {version_id}")
        if hashlib.sha256(content_bytes).hexdigest() != str(version["content_sha256"]):
            raise ValidationError(
                f"{resource['path']} changed while associating — retry the call",
                details={"path": resource["path"], "role": role},
            )
        self.blobs.put(
            namespace=project_id,
            data=content_bytes,
            content_type=str(version["content_type"]),
        )

    def metric_file_sources(
        self,
        *,
        target_id: str,
        attempt_index: int,
        target_type: str = "experiment",
    ) -> list[dict[str, Any]]:
        """Metric-file sources for one attempt's metrics exhibit: role-'result'
        associations matching the metric-file rule whose bytes were pinned at
        associate. Entries carry provenance (path, version, sha, observed_at)
        and the parsed JSON payload."""
        if self.blobs is None:
            return []
        with closing(self.store.connect()) as conn:
            rows = conn.execute(
                """
                SELECT r.path, a.version_id, v.content_sha256, v.observed_at, v.project_id
                FROM resource_associations a
                JOIN resources r ON r.id = a.resource_id
                JOIN resource_versions v ON v.id = a.version_id
                WHERE a.target_type = ? AND a.target_id = ? AND a.role = 'result'
                  AND a.attempt_index = ? AND r.deleted = 0
                ORDER BY r.path
                """,
                (target_type, target_id, int(attempt_index)),
            ).fetchall()
        sources: list[dict[str, Any]] = []
        for row in rows:
            path = str(row["path"])
            if metric_result_capture_cap(role="result", path=path) is None:
                continue
            try:
                data = self.blobs.get(
                    namespace=str(row["project_id"]), sha256=str(row["content_sha256"])
                )
            except NotFoundError:
                continue  # associated without pinned bytes — nothing to ingest
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            sources.append(
                {
                    "path": path,
                    "version_id": str(row["version_id"]),
                    "sha256": str(row["content_sha256"]),
                    "observed_at": str(row["observed_at"]),
                    "data": parsed,
                }
            )
        return sources

    def pin_system_artifact(
        self,
        *,
        path: str,
        target_type: str,
        target_id: str,
        role: str,
        content_bytes: bytes,
        content_type: str = "application/json",
        title: str = "",
        kind: str = "result",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Create/refresh a SYSTEM-authored resource from in-memory bytes and
        pin it to the target's current attempt.

        Deliberately bypasses the agent role vocabulary: the roles the system
        pins (e.g. 'exhibit') are exactly the ones agents must not be able to
        author, replace, or delete — record_observation and delete refuse
        system-owned resources, and association policy rejects the role."""
        if self.blobs is None:
            raise WorkflowError("system artifacts require a configured blob store")
        rel_path = self._repo_relative_path(path=path)
        sha = hashlib.sha256(content_bytes).hexdigest()
        observed_at = now_iso()
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            existing = conn.execute(
                "SELECT * FROM resources WHERE project_id = ? AND path = ?",
                (project_id, rel_path),
            ).fetchone()
            token = f"{rel_path}:system:{sha}"
            if existing:
                resource_id = existing["id"]
                conn.execute(
                    """
                    UPDATE resources
                    SET kind = ?, title = COALESCE(NULLIF(?, ''), title),
                        version_token = ?, mtime_ns = 0, size_bytes = ?,
                        observed_at = ?, missing = 0, deleted = 0,
                        created_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        kind,
                        title,
                        token,
                        len(content_bytes),
                        observed_at,
                        SYSTEM_CREATED_BY,
                        observed_at,
                        resource_id,
                    ),
                )
            else:
                resource_id = new_id(prefix="res")
                conn.execute(
                    """
                    INSERT INTO resources (
                      id, project_id, path, kind, title, version_token, mtime_ns,
                      size_bytes, observed_at, git_commit, missing, created_by,
                      created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, 0, ?, ?, ?)
                    """,
                    (
                        resource_id,
                        project_id,
                        rel_path,
                        kind,
                        title,
                        token,
                        len(content_bytes),
                        observed_at,
                        SYSTEM_CREATED_BY,
                        observed_at,
                        observed_at,
                    ),
                )
            version = self._snapshot_version_record(
                conn=conn,
                resource_id=resource_id,
                project_id=project_id,
                rel_path=rel_path,
                content_sha256=sha,
                size_bytes=len(content_bytes),
                mtime_ns=0,
                content_type=content_type,
                observed_at=observed_at,
                created_by=SYSTEM_CREATED_BY,
            )
            conn.execute(
                "UPDATE resources SET current_version_id = ? WHERE id = ?",
                (version["id"], resource_id),
            )
            self.blobs.put(
                namespace=project_id, data=content_bytes, content_type=content_type
            )
            return self._associate_version(
                conn=conn,
                project_id=project_id,
                resource_id=resource_id,
                version_id=str(version["id"]),
                target_type=target_type,
                target_id=target_id,
                role=role,
            )

    def _capture_submitted_markdown_figures(
        self,
        *,
        conn: Connection,
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
            if not link:
                raise ValidationError("figure link is required")
            if os.path.isabs(link):
                raise ValidationError("figure links must be repo-relative")
            data = figure.get("data")
            if not isinstance(data, bytes):
                continue
            size = len(data)
            if size > MARKDOWN_FIGURE_MAX_BYTES:
                continue
            sha = self.blobs.put(
                namespace=project_id,
                data=data,
                content_type=str(
                    figure.get("content_type") or "application/octet-stream"
                ),
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
        conn: Connection,
        project_id: str,
        resource_id: str,
        version_id: str,
        target_type: str,
        target_id: str,
        role: str,
    ) -> dict[str, Any]:
        target_project_id = self._targets().project_id_for(
            conn=conn,
            target_type=target_type,
            target_id=target_id,
        )
        if target_project_id is not None and target_project_id != project_id:
            raise NotFoundError(
                f"{target_type} not found in project {project_id}: {target_id}"
            )
        attempt_index = self._targets().attempt_index_for(
            conn=conn,
            target_type=target_type,
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
                target_type,
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
            target_type=target_type,
            target_id=target_id,
            payload={
                "resource_id": resource_id,
                "version_id": version_id,
                "role": role,
                "attempt_index": attempt_index,
            },
        )
        return self.resolve(resource_id=resource_id, conn=conn)

    def _snapshot_version_record(
        self,
        *,
        conn: Connection,
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
        row = conn.execute(
            "SELECT * FROM resource_versions WHERE id = ?", (version_id,)
        ).fetchone()
        return self._hydrate_version(row=row, conn=conn)

    def _hydrate_version(self, *, row: Row | None, conn: Connection) -> dict[str, Any]:
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
