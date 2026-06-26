"""Durable sandbox-row persistence for the sandbox registry.

`SandboxRegistry` owns every read and write of the `sandboxes` table (plus the
sandbox event stream). It knows nothing about backends, threads, tunnels, or
rsync — callers hand it row dicts and field updates. The one outward edge is
``on_terminal``: a hook the facade wires so that marking a row failed or
terminated also tears down the row's runtime attachments (dashboard tunnels,
the agent's conn file) without the registry knowing what those are.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from ...sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES
from ...state.store import BaseStateStore, next_created_seq, row_to_dict
from ...utils import NotFoundError, new_id, now_iso


# (experiment_id, sandbox_id, sandbox_uid) — sandbox_id is "" when the row never
# recorded one, and None when the row itself does not exist (the update still ran).
TerminalHook = Callable[[str, str | None, str | None], None]


class SandboxRegistry:
    """Owns sandbox-row persistence: upserts, scoping, status marks, events."""

    def __init__(self, *, store: BaseStateStore) -> None:
        self.store = store
        self.on_terminal: TerminalHook | None = None

    # ---------- reads ----------

    def load_row(self, *, experiment_id: str) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            sandbox_uid = self._primary_uid(
                conn=conn, experiment_id=experiment_id
            ) or self._latest_uid(conn=conn, experiment_id=experiment_id)
            if sandbox_uid is None:
                raise NotFoundError(f"sandbox not found: {experiment_id}")
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE sandbox_uid = ?", (sandbox_uid,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"sandbox not found: {experiment_id}")
            return row_to_dict(row=row) or {}
        finally:
            conn.close()

    def get_by_uid(self, *, sandbox_uid: str) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE sandbox_uid = ?", (sandbox_uid,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"sandbox not found: {sandbox_uid}")
            return row_to_dict(row=row) or {}
        finally:
            conn.close()

    def list_by_experiment(self, *, experiment_id: str) -> list[dict[str, Any]]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE experiment_id = ? ORDER BY created_seq DESC",
                (experiment_id,),
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]
        finally:
            conn.close()

    def tenant_for_project(self, *, project_id: str) -> str:
        """The owning tenant of a project (cloud plan Phase 7), 'local' default."""
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT tenant_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        finally:
            conn.close()
        return str(row["tenant_id"]) if row is not None else "local"

    def fetch_scoped(
        self,
        *,
        experiment_id: str | None,
        project_id: str | None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            if project_id is not None or tenant_id is not None:
                project_id = self.store.require_project_id(
                    conn=conn, project_id=project_id, tenant_id=tenant_id
                )
            target_uid = (sandbox_uid or "").strip()
            if target_uid:
                row = conn.execute(
                    "SELECT * FROM sandboxes WHERE sandbox_uid = ?", (target_uid,)
                ).fetchone()
            else:
                if not experiment_id:
                    raise NotFoundError("sandbox_uid or experiment_id is required")
                target_uid = (
                    self._primary_uid(conn=conn, experiment_id=experiment_id)
                    or self._latest_uid(conn=conn, experiment_id=experiment_id)
                    or ""
                )
                row = (
                    conn.execute(
                        "SELECT * FROM sandboxes WHERE sandbox_uid = ?", (target_uid,)
                    ).fetchone()
                    if target_uid
                    else None
                )
            if row is None:
                if target_uid:
                    raise NotFoundError(f"sandbox not found: {target_uid}")
                raise NotFoundError(f"no sandbox for experiment: {experiment_id}")
            if experiment_id and row["experiment_id"] != experiment_id:
                raise NotFoundError(f"no sandbox for experiment: {experiment_id}")
            if project_id is not None and row["project_id"] != project_id:
                raise NotFoundError(
                    f"sandbox not found in project {project_id}: {experiment_id}"
                )
            return row_to_dict(row=row) or {}
        finally:
            conn.close()

    def exists(self, *, experiment_id: str) -> bool:
        conn = self.store.connect()
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
                ).fetchone()
                is not None
            )
        finally:
            conn.close()

    def list_rows(self, *, project_id: str | None) -> list[dict[str, Any]]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE project_id = ? ORDER BY created_seq DESC",
                (project_id,),
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]
        finally:
            conn.close()

    def list_running_rows(self) -> list[dict[str, Any]]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE status = 'running' ORDER BY created_seq DESC"
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]
        finally:
            conn.close()

    def list_rows_by_status(self, *, status: str) -> list[dict[str, Any]]:
        """All sandbox rows (across tenants/projects) in ``status``.

        The cross-project read the cloud cleanup sweeps need: the orphan-VM and
        stale-provision reapers reconcile every running/provisioning row, not a
        single project's. Local mode (one project) gets the same rows it always
        did.
        """
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE status = ? ORDER BY created_seq DESC",
                (status,),
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]
        finally:
            conn.close()

    def experiment_name(self, *, experiment_id: str) -> str:
        """The experiment's short folder name; '' on rows that predate it."""
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT name FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
        finally:
            conn.close()
        return str(row["name"]) if row is not None and row["name"] else ""

    def _primary_uid(self, *, conn: Any, experiment_id: str) -> str | None:
        """Most recent running sandbox for experiment-keyed reads. Callers fall
        back to _latest_uid, so together they reproduce the prior running →
        newest-of-any resolution (a lone provisioning row stays reachable)."""
        statuses = tuple(ACTIVE_SANDBOX_STATUSES)
        if not statuses:
            return None
        placeholders = ", ".join("?" for _ in statuses)
        row = conn.execute(
            f"""
            SELECT sandbox_uid FROM sandboxes
            WHERE experiment_id = ? AND status IN ({placeholders})
            ORDER BY created_seq DESC
            LIMIT 1
            """,
            (experiment_id, *statuses),
        ).fetchone()
        return str(row["sandbox_uid"]) if row is not None and row["sandbox_uid"] else None

    def _latest_uid(self, *, conn: Any, experiment_id: str) -> str | None:
        """Newest sandbox of any status — the experiment-keyed read fallback when
        none is running, so terminal/provisioning rows stay reachable."""
        row = conn.execute(
            """
            SELECT sandbox_uid FROM sandboxes
            WHERE experiment_id = ?
            ORDER BY created_seq DESC
            LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()
        return str(row["sandbox_uid"]) if row is not None and row["sandbox_uid"] else None

    def has_active_for_experiment(
        self, *, experiment_id: str, exclude_sandbox_uid: str | None = None
    ) -> bool:
        """Whether another live row keeps the experiment running."""
        # A still-provisioning sibling keeps the experiment alive too: reaping one
        # running box while another is mid-boot must not revert the experiment.
        statuses = tuple({*ACTIVE_SANDBOX_STATUSES, "provisioning"})
        if not statuses:
            return False
        placeholders = ", ".join("?" for _ in statuses)
        params: list[Any] = [experiment_id, *statuses]
        clause = ""
        exclude = (exclude_sandbox_uid or "").strip()
        if exclude:
            clause = "AND sandbox_uid != ?"
            params.append(exclude)
        conn = self.store.connect()
        try:
            row = conn.execute(
                f"""
                SELECT 1 FROM sandboxes
                WHERE experiment_id = ? AND status IN ({placeholders}) {clause}
                LIMIT 1
                """,
                params,
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ---------- writes ----------

    def new_sandbox_uid(self) -> str:
        return uuid.uuid4().hex

    def upsert(
        self,
        *,
        experiment_id: str,
        sandbox_uid: str,
        **fields: Any,
    ) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            target_uid = str(sandbox_uid or "").strip()
            if not target_uid:
                raise ValueError("sandbox_uid is required")
            exists = conn.execute(
                "SELECT sandbox_uid FROM sandboxes WHERE sandbox_uid = ?",
                (target_uid,),
            ).fetchone()
            payload = dict(fields)
            if payload.get("project_id") and not payload.get("tenant_id"):
                tenant_row = conn.execute(
                    "SELECT tenant_id FROM projects WHERE id = ?",
                    (payload["project_id"],),
                ).fetchone()
                payload["tenant_id"] = (
                    str(tenant_row["tenant_id"]) if tenant_row is not None else "local"
                )
            payload["updated_at"] = now
            if exists is None:
                payload["experiment_id"] = experiment_id
                payload["sandbox_uid"] = target_uid
                payload.setdefault("created_at", now)
                # Insertion-order column (cloud plan Phase 6): replaces rowid
                # ordering for the most-recent-first sandbox listings.
                payload["created_seq"] = next_created_seq(conn=conn, table="sandboxes")
                columns = ", ".join(payload)
                placeholders = ", ".join("?" for _ in payload)
                conn.execute(
                    f"INSERT INTO sandboxes ({columns}) VALUES ({placeholders})",
                    list(payload.values()),
                )
                self._ensure_attachment(
                    conn=conn,
                    sandbox_uid=str(payload["sandbox_uid"]),
                    experiment_id=experiment_id,
                    attached_at=str(payload["created_at"]),
                )
            else:
                sandbox_uid = str(exists["sandbox_uid"] or target_uid)
                assignments = ", ".join(f"{key} = ?" for key in payload)
                conn.execute(
                    f"UPDATE sandboxes SET {assignments} WHERE sandbox_uid = ?",
                    [*payload.values(), sandbox_uid],
                )
                if sandbox_uid and str(payload.get("status") or "") not in {
                    "",
                    "terminated",
                    "failed",
                }:
                    self._ensure_attachment(
                        conn=conn,
                        sandbox_uid=sandbox_uid,
                        experiment_id=experiment_id,
                        attached_at=now,
                    )

    def create_sandbox(self, *, experiment_id: str, **fields: Any) -> str:
        """Insert a distinct row for a parallel sandbox under the experiment."""
        sandbox_uid = str(fields.pop("sandbox_uid", "") or self.new_sandbox_uid())
        self.upsert(experiment_id=experiment_id, sandbox_uid=sandbox_uid, **fields)
        return sandbox_uid

    def provision_additional(self, *, experiment_id: str, **fields: Any) -> str:
        return self.create_sandbox(experiment_id=experiment_id, **fields)

    def reattach(
        self,
        *,
        sandbox_uid: str,
        experiment_id: str,
        project_id: str,
        workdir: str,
        sync_dir: str,
        sandbox_data_dir: str,
        unsynced_dir: str,
        mgmt_key_ref: str,
    ) -> dict[str, Any]:
        """Atomically move one live sandbox row to another current experiment."""
        now = now_iso()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE sandbox_uid = ?", (sandbox_uid,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"sandbox not found: {sandbox_uid}")
            source_experiment_id = str(row["experiment_id"] or "")
            tenant_row = conn.execute(
                "SELECT tenant_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            tenant_id = str(tenant_row["tenant_id"]) if tenant_row is not None else "local"
            self._close_attachment(
                conn=conn,
                sandbox_uid=sandbox_uid,
                experiment_id=source_experiment_id,
                detached_at=now,
            )
            self._ensure_attachment(
                conn=conn,
                sandbox_uid=sandbox_uid,
                experiment_id=experiment_id,
                attached_at=now,
            )
            conn.execute(
                """
                UPDATE sandboxes
                SET experiment_id = ?, project_id = ?, tenant_id = ?,
                    workdir = ?, sync_dir = ?, sandbox_data_dir = ?,
                    unsynced_dir = ?, mgmt_key_ref = ?, phase = '', detail = '',
                    error = '', updated_at = ?
                WHERE sandbox_uid = ?
                """,
                (
                    experiment_id,
                    project_id,
                    tenant_id,
                    workdir,
                    sync_dir,
                    sandbox_data_dir,
                    unsynced_dir,
                    mgmt_key_ref,
                    now,
                    sandbox_uid,
                ),
            )
            fresh = conn.execute(
                "SELECT * FROM sandboxes WHERE sandbox_uid = ?", (sandbox_uid,)
            ).fetchone()
            return row_to_dict(row=fresh) or {}

    def record_generation(
        self,
        *,
        experiment_id: str,
        project_id: str,
        sandbox_id: str = "",
        instance_type: str = "",
        gpu: str = "",
        price_usd_per_hour: float = 0.0,
    ) -> str:
        """Append a per-generation spend-ledger row (cloud plan Phase 7).

        The sandboxes row retains only its current generation, so historical
        spend cannot be reconstructed from it. Each provisioned generation lands
        here with its provider price quote and the tenant (reached through the
        project) so total spend is reconstructable. Always recorded; in local
        mode the 'local' tenant simply has no quota to govern it.
        """
        generation_id = new_id(prefix="sbg")
        now = now_iso()
        with self.store.transaction() as conn:
            tenant_row = conn.execute(
                "SELECT tenant_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            tenant_id = str(tenant_row["tenant_id"]) if tenant_row is not None else "local"
            conn.execute(
                """
                INSERT INTO sandbox_generations (
                  id, experiment_id, project_id, tenant_id, sandbox_id,
                  instance_type, gpu, price_usd_per_hour, started_at, created_seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    experiment_id,
                    project_id,
                    tenant_id,
                    sandbox_id,
                    instance_type,
                    gpu,
                    price_usd_per_hour,
                    now,
                    next_created_seq(conn=conn, table="sandbox_generations"),
                ),
            )
        return generation_id

    def close_generation(
        self,
        *,
        experiment_id: str,
        sandbox_id: str | None = None,
        now: str | None = None,
    ) -> None:
        """Stamp ``ended_at`` on this experiment's open generation(s).

        Cost governance (cloud plan Phase 9): an open generation (``ended_at IS
        NULL``) is billed to "now" by the spend accountant; closing it on
        termination freezes its runtime so the running total stops climbing.
        Idempotent — already-closed generations are untouched. Best-effort and
        clock-injectable (the reaper passes its own ``now``).
        """
        closed_at = now or now_iso()
        with self.store.transaction() as conn:
            if sandbox_id:
                conn.execute(
                    "UPDATE sandbox_generations SET ended_at = ? "
                    "WHERE experiment_id = ? AND sandbox_id = ? AND ended_at IS NULL",
                    (closed_at, experiment_id, sandbox_id),
                )
            else:
                conn.execute(
                    "UPDATE sandbox_generations SET ended_at = ? "
                    "WHERE experiment_id = ? AND ended_at IS NULL",
                    (closed_at, experiment_id),
                )

    def touch_alive(self, *, experiment_id: str, sandbox_uid: str) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            target_uid = str(sandbox_uid or "").strip()
            if not target_uid:
                return
            conn.execute(
                "UPDATE sandboxes SET last_seen_at = ?, updated_at = ? WHERE sandbox_uid = ?",
                (now, now, target_uid),
            )

    def heartbeat_snapshot(self, *, row: dict[str, Any]) -> dict[str, Any] | None:
        try:
            data = json.loads(str(row.get("heartbeat_snapshot_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def record_heartbeat(
        self,
        *,
        experiment_id: str,
        sandbox_uid: str,
        idle_since: str | None,
        snapshot: dict[str, Any],
    ) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            target_uid = str(sandbox_uid or "").strip()
            if not target_uid:
                return
            conn.execute(
                """
                UPDATE sandboxes
                SET idle_since = ?, heartbeat_snapshot_json = ?, updated_at = ?
                WHERE sandbox_uid = ?
                """,
                (
                    idle_since,
                    json.dumps(snapshot, sort_keys=True),
                    now,
                    target_uid,
                ),
            )

    def mark_terminated(self, *, experiment_id: str, sandbox_uid: str) -> None:
        self._mark_terminal(
            experiment_id=experiment_id, sandbox_uid=sandbox_uid, status="terminated"
        )

    def mark_failed(self, *, experiment_id: str, error: str, sandbox_uid: str) -> None:
        self._mark_terminal(
            experiment_id=experiment_id,
            sandbox_uid=sandbox_uid,
            status="failed",
            error=error,
        )

    def _mark_terminal(
        self,
        *,
        experiment_id: str,
        sandbox_uid: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Drive one sandbox row to a terminal status, closing its attachment
        and spend generation. `error` is set only on the failed path."""
        now = now_iso()
        with self.store.transaction() as conn:
            target_uid = str(sandbox_uid or "").strip()
            row = (
                conn.execute(
                    "SELECT sandbox_id, sandbox_uid FROM sandboxes WHERE sandbox_uid = ?",
                    (target_uid,),
                ).fetchone()
                if target_uid
                else None
            )
            sandbox_id = str(row["sandbox_id"] or "") if row is not None else None
            row_uid = str(row["sandbox_uid"] or "") if row is not None else target_uid
            if error is None:
                conn.execute(
                    """
                    UPDATE sandboxes
                    SET status = ?, terminated_at = ?, updated_at = ?
                    WHERE sandbox_uid = ?
                    """,
                    (status, now, now, row_uid),
                )
            else:
                conn.execute(
                    """
                    UPDATE sandboxes
                    SET status = ?, error = ?, phase = '', detail = '',
                        terminated_at = ?, updated_at = ?
                    WHERE sandbox_uid = ?
                    """,
                    (status, error, now, now, row_uid),
                )
            if row is not None:
                self._close_attachment(
                    conn=conn,
                    sandbox_uid=row_uid,
                    experiment_id=experiment_id,
                    detached_at=now,
                )
        # Only a recorded provider id can identify this row's spend generation.
        if sandbox_id:
            self.close_generation(
                experiment_id=experiment_id, sandbox_id=sandbox_id, now=now
            )
        elif not row_uid:
            self.close_generation(experiment_id=experiment_id, now=now)
        self._fire_terminal(
            experiment_id=experiment_id, sandbox_id=sandbox_id, sandbox_uid=row_uid
        )

    def emit_event(
        self,
        *,
        project_id: str,
        event_type: str,
        experiment_id: str,
        payload: dict[str, Any],
    ) -> None:
        with self.store.transaction() as conn:
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type=event_type,
                target_type="sandbox",
                target_id=experiment_id or str(payload.get("sandbox_uid") or ""),
                payload=payload,
            )

    # ---------- terminal hook plumbing ----------

    def _ensure_attachment(
        self,
        *,
        conn: Any,
        sandbox_uid: str,
        experiment_id: str,
        attached_at: str,
    ) -> None:
        if not sandbox_uid or not experiment_id:
            return
        conn.execute(
            """
            INSERT INTO sandbox_attachments (
              sandbox_uid, experiment_id, attached_at, detached_at
            )
            SELECT ?, ?, ?, NULL
            WHERE NOT EXISTS (
              SELECT 1 FROM sandbox_attachments
              WHERE sandbox_uid = ? AND experiment_id = ? AND detached_at IS NULL
            )
            """,
            (sandbox_uid, experiment_id, attached_at, sandbox_uid, experiment_id),
        )

    def _close_attachment(
        self,
        *,
        conn: Any,
        sandbox_uid: str,
        experiment_id: str,
        detached_at: str,
    ) -> None:
        if not sandbox_uid or not experiment_id:
            return
        conn.execute(
            """
            UPDATE sandbox_attachments
            SET detached_at = ?
            WHERE sandbox_uid = ? AND experiment_id = ? AND detached_at IS NULL
            """,
            (detached_at, sandbox_uid, experiment_id),
        )

    def _fire_terminal(
        self,
        *,
        experiment_id: str,
        sandbox_id: str | None,
        sandbox_uid: str | None,
    ) -> None:
        if self.on_terminal is None:
            return
        try:
            self.on_terminal(experiment_id, sandbox_id, sandbox_uid)
        except Exception:  # noqa: BLE001 — teardown must never block the mark
            pass
