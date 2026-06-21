"""Durable sandbox-row persistence for the sandbox registry.

`SandboxRegistry` owns every read and write of the `sandboxes` table (plus the
sandbox event stream). It knows nothing about backends, threads, tunnels, or
rsync — callers hand it row dicts and field updates. The one outward edge is
``on_terminal``: a hook the facade wires so that marking a row failed or
terminated also tears down the row's runtime attachments (dashboard tunnels,
the agent's conn file) without the registry knowing what those are.
"""

from __future__ import annotations

from typing import Any, Callable

from ...ports.sandbox_sync import RunningSandboxSyncRow
from ...state.store import BaseStateStore, next_created_seq, row_to_dict
from ...utils import NotFoundError, new_id, now_iso


# (experiment_id, sandbox_id) — sandbox_id is "" when the row never recorded
# one, and None when the row itself does not exist (the update still ran).
TerminalHook = Callable[[str, str | None], None]


class SandboxRegistry:
    """Owns sandbox-row persistence: upserts, scoping, status marks, events."""

    def __init__(self, *, store: BaseStateStore) -> None:
        self.store = store
        self.on_terminal: TerminalHook | None = None

    # ---------- reads ----------

    def load_row(self, *, experiment_id: str) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"sandbox not found: {experiment_id}")
            return row_to_dict(row=row) or {}
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
        experiment_id: str,
        project_id: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            if project_id is not None or tenant_id is not None:
                project_id = self.store.require_project_id(
                    conn=conn, project_id=project_id, tenant_id=tenant_id
                )
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            if row is None:
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

    def list_running_sync_rows(self) -> list[RunningSandboxSyncRow]:
        """Running sandbox rows projected to the sync-session contract."""
        conn = self.store.connect()
        try:
            rows = conn.execute(
                """
                SELECT experiment_id, tenant_id, sandbox_id, ssh_host, ssh_port,
                       ssh_user, sync_dir, workdir, sandbox_data_dir, unsynced_dir
                FROM sandboxes
                WHERE status = 'running'
                ORDER BY created_seq DESC
                """
            ).fetchall()
            return [
                RunningSandboxSyncRow(
                    experiment_id=str(row["experiment_id"] or ""),
                    tenant_id=row["tenant_id"],
                    sandbox_id=row["sandbox_id"],
                    ssh_host=row["ssh_host"],
                    ssh_port=row["ssh_port"],
                    ssh_user=row["ssh_user"],
                    sync_dir=row["sync_dir"],
                    workdir=row["workdir"],
                    sandbox_data_dir=row["sandbox_data_dir"],
                    unsynced_dir=row["unsynced_dir"],
                )
                for row in rows
            ]
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

    # ---------- writes ----------

    def upsert(self, *, experiment_id: str, **fields: Any) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
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
            else:
                assignments = ", ".join(f"{key} = ?" for key in payload)
                conn.execute(
                    f"UPDATE sandboxes SET {assignments} WHERE experiment_id = ?",
                    [*payload.values(), experiment_id],
                )

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

        The sandboxes row is upsert-overwritten per experiment, so historical
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

    def close_generation(self, *, experiment_id: str, now: str | None = None) -> None:
        """Stamp ``ended_at`` on this experiment's open generation(s).

        Cost governance (cloud plan Phase 9): an open generation (``ended_at IS
        NULL``) is billed to "now" by the spend accountant; closing it on
        termination freezes its runtime so the running total stops climbing.
        Idempotent — already-closed generations are untouched. Best-effort and
        clock-injectable (the reaper passes its own ``now``).
        """
        closed_at = now or now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE sandbox_generations SET ended_at = ? "
                "WHERE experiment_id = ? AND ended_at IS NULL",
                (closed_at, experiment_id),
            )

    def touch_alive(self, *, experiment_id: str) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET last_seen_at = ?, updated_at = ? WHERE experiment_id = ?",
                (now, now, experiment_id),
            )

    def mark_terminated(self, *, experiment_id: str) -> None:
        sandbox_id = self._sandbox_id_or_none(experiment_id=experiment_id)
        now = now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE sandboxes
                SET status = 'terminated', terminated_at = ?, updated_at = ?
                WHERE experiment_id = ?
                """,
                (now, now, experiment_id),
            )
        # Freeze the spend ledger: a terminated generation stops accruing.
        self.close_generation(experiment_id=experiment_id, now=now)
        self._fire_terminal(experiment_id=experiment_id, sandbox_id=sandbox_id)

    def mark_failed(self, *, experiment_id: str, error: str) -> None:
        sandbox_id = self._sandbox_id_or_none(experiment_id=experiment_id)
        now = now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE sandboxes
                SET status = 'failed', error = ?, phase = '', detail = '',
                    terminated_at = ?, updated_at = ?
                WHERE experiment_id = ?
                """,
                (error, now, now, experiment_id),
            )
        # Freeze the spend ledger: a failed generation stops accruing.
        self.close_generation(experiment_id=experiment_id, now=now)
        self._fire_terminal(experiment_id=experiment_id, sandbox_id=sandbox_id)

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
                target_id=experiment_id,
                payload=payload,
            )

    # ---------- terminal hook plumbing ----------

    def _sandbox_id_or_none(self, *, experiment_id: str) -> str | None:
        try:
            row = self.load_row(experiment_id=experiment_id)
        except NotFoundError:
            return None
        return str(row.get("sandbox_id") or "")

    def _fire_terminal(self, *, experiment_id: str, sandbox_id: str | None) -> None:
        if self.on_terminal is None:
            return
        try:
            self.on_terminal(experiment_id, sandbox_id)
        except Exception:  # noqa: BLE001 — teardown must never block the mark
            pass
