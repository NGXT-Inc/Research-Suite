"""Pinned-artifact loading: association → version → blob bytes.

Decision 6 of docs/CLOUD_BACKEND_MIGRATION_PLAN.md (Phase 2): workflow gates
lint the bytes that were SUBMITTED at ``resource.associate`` — pinned to a
version and stored in the blob store — never the live working tree. Fixing a
gated artifact therefore means fix the file and re-associate it; editing the
file alone changes nothing the gates can see.

There is deliberately no disk fallback here: a missing blob raises a
WorkflowError telling the agent to re-associate, so a partially-migrated
database degrades into actionable guidance instead of silently linting stale
or live content.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..state.blobs import BlobStore
from ..utils import NotFoundError, WorkflowError


def resubmit_hint(*, role: str, path: str) -> str:
    return (
        f"re-associate it (resource.associate with role {role!r}) to submit "
        f"the current content of {path}"
    )


def pinned_artifact_text(
    *,
    conn: sqlite3.Connection,
    blobs: BlobStore,
    target_type: str,
    target_id: str,
    role: str,
    attempt_index: int,
    what: str,
) -> tuple[str, str, str]:
    """(text, version_id, path) of the newest current-attempt association.

    Loads the association's pinned version and fetches its bytes from the
    blob store. Raises WorkflowError with re-associate guidance when the
    association, pin, or blob is absent.
    """
    row = conn.execute(
        """
        SELECT a.version_id, r.path, r.project_id, v.content_sha256
        FROM resource_associations a
        JOIN resources r ON r.id = a.resource_id
        LEFT JOIN resource_versions v ON v.id = a.version_id
        WHERE a.target_type = ? AND a.target_id = ? AND a.role = ?
          AND a.attempt_index = ? AND r.deleted = 0
        ORDER BY a.created_seq DESC
        LIMIT 1
        """,
        (target_type, target_id, role, attempt_index),
    ).fetchone()
    if row is None:
        raise WorkflowError(
            f"no {role!r} resource is associated for the current attempt"
        )
    path = str(row["path"])
    if not row["version_id"] or not row["content_sha256"]:
        raise WorkflowError(
            f"{what} ({path}) has no pinned version — "
            + resubmit_hint(role=role, path=path)
        )
    text = pinned_text_for_version(
        conn=conn,
        blobs=blobs,
        version_id=str(row["version_id"]),
        what=what,
        role=role,
    )
    return text, str(row["version_id"]), path


def pinned_text_for_version(
    *,
    conn: sqlite3.Connection,
    blobs: BlobStore,
    version_id: str,
    what: str,
    role: str,
) -> str:
    """The submitted text of one pinned resource version, from the blob store."""
    version = conn.execute(
        "SELECT project_id, path, content_sha256 FROM resource_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    if version is None:
        raise WorkflowError(f"{what}: resource version not found: {version_id}")
    path = str(version["path"])
    try:
        data = blobs.get(
            namespace=str(version["project_id"]),
            sha256=str(version["content_sha256"]),
        )
    except NotFoundError as exc:
        raise WorkflowError(
            f"{what} ({path}) has no submitted content — "
            + resubmit_hint(role=role, path=path)
        ) from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowError(
            f"{what} ({path}) is not valid UTF-8 text"
        ) from exc


def pinned_version_row(
    *,
    conn: sqlite3.Connection,
    target_type: str,
    target_id: str,
    role: str,
    attempt_index: int,
) -> dict[str, Any] | None:
    """The newest current-attempt association row for ``role`` (or None)."""
    row = conn.execute(
        """
        SELECT a.version_id, r.path, r.project_id
        FROM resource_associations a
        JOIN resources r ON r.id = a.resource_id
        WHERE a.target_type = ? AND a.target_id = ? AND a.role = ?
          AND a.attempt_index = ? AND r.deleted = 0
        ORDER BY a.created_seq DESC
        LIMIT 1
        """,
        (target_type, target_id, role, attempt_index),
    ).fetchone()
    if row is None:
        return None
    return {
        "version_id": str(row["version_id"]) if row["version_id"] else None,
        "path": str(row["path"]),
        "project_id": str(row["project_id"]),
    }
