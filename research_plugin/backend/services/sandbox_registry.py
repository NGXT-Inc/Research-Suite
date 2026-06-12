"""Durable sandbox-row persistence for the sandbox registry.

`SandboxRegistry` owns every read and write of the `sandboxes` table (plus the
sandbox event stream). It knows nothing about backends, threads, tunnels, or
rsync — callers hand it row dicts and field updates. The one outward edge is
``on_terminal``: a hook the facade wires so that marking a row failed or
terminated also tears down the row's runtime attachments (dashboard tunnels,
the agent's conn file) without the registry knowing what those are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..workspace import local_experiment_dir
from ..state.store import StateStore, row_to_dict
from ..utils import NotFoundError, now_iso


# (experiment_id, sandbox_id) — sandbox_id is "" when the row never recorded
# one, and None when the row itself does not exist (the update still ran).
TerminalHook = Callable[[str, str | None], None]


class SandboxRegistry:
    """Owns sandbox-row persistence: upserts, scoping, status marks, events."""

    def __init__(self, *, store: StateStore) -> None:
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

    def fetch_scoped(
        self, *, experiment_id: str, project_id: str | None
    ) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            if project_id is not None:
                project_id = self.store.require_project_id(
                    conn=conn, project_id=project_id
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
                "SELECT * FROM sandboxes WHERE project_id = ? ORDER BY rowid DESC",
                (project_id,),
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]
        finally:
            conn.close()

    def list_running_rows(self) -> list[dict[str, Any]]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE status = 'running' ORDER BY rowid DESC"
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

    def local_sync_dir(self, *, experiment_id: str) -> Path:
        return local_experiment_dir(
            repo_root=self.store.repo_root,
            experiment_id=experiment_id,
            name=self.experiment_name(experiment_id=experiment_id),
        )

    # ---------- writes ----------

    def upsert(self, *, experiment_id: str, **fields: Any) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            payload = dict(fields)
            payload["updated_at"] = now
            if exists is None:
                payload["experiment_id"] = experiment_id
                payload.setdefault("created_at", now)
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
