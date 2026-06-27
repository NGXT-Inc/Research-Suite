"""Local→cloud onboarding import (cloud plan Phase 8).

Reads a local repo's ``.research_plugin/state.sqlite`` and imports its
projects / claims / experiments / resources / reviews / syntheses into a
tenant-scoped cloud store. Rules from the plan:

- Entity ids carry over (prefix+uuid strings are tenant-wide unique).
- ``events.id`` (the only AUTOINCREMENT id) is re-keyed order-preserving on
  insert; nothing FK-references it, so the rewrite is safe.
- Tenancy: every imported project is stamped with the target ``tenant_id``.
- Gated-artifact blob backfill ONLY where the working-tree file still matches
  the pinned ``content_sha256`` (the Phase 2 backfill rule reused); everything
  else imports metadata-only, to be re-associated under the new model.
- Preconditions: NO open review requests and NO running sandboxes at flip.
- A one-way tombstone is written into the local store so the two modes cannot
  silently diverge afterward (the local daemon refuses to mutate a tombstoned
  store).

The test exercises sqlite→sqlite (tenant scoping is what matters); a real run
points ``target_store`` at a PostgresStateStore.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from ..state.blobs import BlobStore
from ..state.store import BaseStateStore
from ..utils import ValidationError, now_iso


# Tables copied verbatim (ids carry over), in FK-safe order. events is handled
# separately (id re-keyed). Machine-local tables (sandboxes,
# sandbox_generations) are NOT imported — they are per-VM / per-machine and
# meaningless in the cloud; the cloud re-provisions.
_COPY_TABLES = (
    "claims",
    "experiments",
    "experiment_claims",
    "resources",
    "resource_versions",
    "resource_associations",
    "review_requests",
    "review_sessions",
    "reviews",
    "syntheses",
    "report_figures",
)

_GATED_ROLES = (
    "plan",
    "report",
    "graph",
    "project_graph",
    "reflection_doc",
    "reflection_lens_doc",
    "synthesis_doc",
    "change_spec",
    "proposals",
    "reflection",
)

# Marker key written into the local store's schema_migrations-style meta so a
# re-import or a daemon mutation can detect the one-way flip.
TOMBSTONE_TABLE = "import_tombstone"


class ImportResult(dict):
    """Plain dict result with named accessors for readability in tests."""


def import_local_to_cloud(
    *,
    local_db_path: Path,
    repo_root: Path,
    target_store: BaseStateStore,
    tenant_id: str,
    target_blobs: BlobStore | None = None,
) -> dict[str, Any]:
    """Import one local repo's records into a tenant-scoped cloud store.

    Returns a summary ``{projects, claims, experiments, resources, reviews,
    syntheses, events, blobs_backfilled}``. Raises ValidationError on a failed
    precondition (open reviews / running sandboxes) or a re-import attempt.
    """
    if not local_db_path.exists():
        raise ValidationError(f"local state store not found: {local_db_path}")
    src = sqlite3.connect(local_db_path)
    src.row_factory = sqlite3.Row
    try:
        _assert_not_already_imported(src)
        _assert_preconditions(src)
        summary = _copy_records(
            src=src, target_store=target_store, tenant_id=tenant_id
        )
        if target_blobs is not None:
            summary["blobs_backfilled"] = _backfill_gated_blobs(
                src=src, repo_root=repo_root, target_blobs=target_blobs
            )
        else:
            summary["blobs_backfilled"] = 0
        _write_tombstone(src, tenant_id=tenant_id)
        src.commit()
    finally:
        src.close()
    return summary


def _assert_not_already_imported(src: sqlite3.Connection) -> None:
    row = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (TOMBSTONE_TABLE,),
    ).fetchone()
    if row is not None:
        marker = src.execute(
            f"SELECT tenant_id, imported_at FROM {TOMBSTONE_TABLE} LIMIT 1"
        ).fetchone()
        raise ValidationError(
            "this local store was already imported to the cloud "
            f"(tenant={marker['tenant_id'] if marker else '?'}); "
            "the import is one-way — refusing to re-import",
            details={"tombstone": dict(marker) if marker else {}},
        )


def _assert_preconditions(src: sqlite3.Connection) -> None:
    # No open review requests (an in-flight review would be orphaned by the
    # flip). A request is open while still 'requested' (not yet completed); the
    # capability lifecycle never reuses 'requested' for a closed review.
    open_reviews = src.execute(
        "SELECT COUNT(*) AS n FROM review_requests WHERE status = 'requested'"
    ).fetchone()["n"]
    if open_reviews:
        raise ValidationError(
            f"cannot import: {open_reviews} open review request(s) — resolve them first",
            details={"open_reviews": int(open_reviews)},
        )
    # No running/provisioning sandboxes (the VM + its bytes would be stranded).
    running = src.execute(
        "SELECT COUNT(*) AS n FROM sandboxes WHERE status IN ('running', 'provisioning')"
    ).fetchone()["n"]
    if running:
        raise ValidationError(
            f"cannot import: {running} running/provisioning sandbox(es) — "
            "release them first",
            details={"running_sandboxes": int(running)},
        )


def _copy_records(
    *, src: sqlite3.Connection, target_store: BaseStateStore, tenant_id: str
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    with target_store.transaction() as dst:
        # Projects first (FK parent), re-tenanted.
        projects = [dict(r) for r in src.execute("SELECT * FROM projects").fetchall()]
        for project in projects:
            project = dict(project)
            project["tenant_id"] = tenant_id
            _insert_row(dst, table="projects", row=project)
        summary["projects"] = len(projects)
        # Ensure the tenant row exists so FK/scoping is consistent.
        _ensure_tenant(dst, tenant_id=tenant_id)

        for table in _COPY_TABLES:
            rows = _maybe_select_all(src, table)
            for row in rows:
                _insert_row(dst, table=table, row=dict(row))
            summary[table] = len(rows)

        # events: re-key the AUTOINCREMENT id order-preserving (nothing FKs it).
        events = _maybe_select_all(src, "events", order_by="id")
        summary["events"] = 0
        for row in events:
            payload = dict(row)
            payload.pop("id", None)  # let the target assign a fresh ordered id
            _insert_row(dst, table="events", row=payload)
            summary["events"] += 1
    # Friendlier summary aliases the test reads.
    summary["claims"] = summary.get("claims", 0)
    summary["experiments"] = summary.get("experiments", 0)
    summary["resources"] = summary.get("resources", 0)
    summary["reviews"] = summary.get("reviews", 0)
    summary["syntheses"] = summary.get("syntheses", 0)
    return summary


def _backfill_gated_blobs(
    *, src: sqlite3.Connection, repo_root: Path, target_blobs: BlobStore
) -> int:
    """Capture gated-artifact bytes ONLY where the working-tree file still
    matches the pinned content_sha256 (the Phase 2 backfill rule). Everything
    else imports metadata-only — the gates surface re-associate guidance."""
    placeholders = ",".join("?" * len(_GATED_ROLES))
    rows = src.execute(
        f"""
        SELECT DISTINCT v.project_id, v.path, v.content_sha256, v.content_type
        FROM resource_associations a
        JOIN resource_versions v ON v.id = a.version_id
        WHERE a.role IN ({placeholders})
        """,
        _GATED_ROLES,
    ).fetchall()
    backfilled = 0
    for row in rows:
        namespace = str(row["project_id"])
        sha = str(row["content_sha256"])
        if target_blobs.stat(namespace=namespace, sha256=sha) is not None:
            continue
        full = repo_root / str(row["path"])
        try:
            data = full.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() != sha:
            continue  # working tree drifted from the pin — metadata-only
        target_blobs.put(
            namespace=namespace, data=data, content_type=str(row["content_type"])
        )
        backfilled += 1
    return backfilled


def _write_tombstone(src: sqlite3.Connection, *, tenant_id: str) -> None:
    src.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TOMBSTONE_TABLE} (
          tenant_id TEXT NOT NULL,
          imported_at TEXT NOT NULL
        )
        """
    )
    src.execute(
        f"INSERT INTO {TOMBSTONE_TABLE} (tenant_id, imported_at) VALUES (?, ?)",
        (tenant_id, now_iso()),
    )


def is_tombstoned(local_db_path: Path) -> bool:
    """Whether a local store has been imported to the cloud (one-way flip).

    The local daemon checks this before mutating so the two modes cannot
    silently diverge after onboarding.
    """
    if not local_db_path.exists():
        return False
    conn = sqlite3.connect(local_db_path)
    try:
        return (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (TOMBSTONE_TABLE,),
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def _ensure_tenant(dst, *, tenant_id: str) -> None:
    row = dst.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if row is None:
        dst.execute(
            "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
            (tenant_id, tenant_id, now_iso()),
        )


def _maybe_select_all(
    src: sqlite3.Connection, table: str, *, order_by: str | None = None
) -> list[sqlite3.Row]:
    exists = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return []
    sql = f"SELECT * FROM {table}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    return src.execute(sql).fetchall()


def _insert_row(dst, *, table: str, row: dict[str, Any]) -> None:
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    col_sql = ", ".join(columns)
    dst.execute(
        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
        [row[c] for c in columns],
    )
