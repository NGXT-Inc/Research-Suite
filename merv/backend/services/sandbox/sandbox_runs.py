"""Brain-side mirror of merv_run receipts: reconcile, persist, notify.

The sandbox filesystem is the registry (merv_run writes .runs/<label>/ sentinel
files); this ledger pulls that state over the management channel the brain
already holds and keeps the `sandbox_runs` table as its durable mirror, so a
run's outcome survives the agent session AND the sandbox. run.finished is
emitted exactly once per run — the emitted flag and the event flip in one
transaction, so daemon restarts cannot double-fire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ...sandbox.sandbox_backend import SandboxBackend
from ...sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES
from ...ports.mgmt_keys import MgmtKeyStore
from ...state.store import BaseStateStore, row_to_dict
from ...utils import now_iso, parse_iso
from .sandbox_registry import SandboxRegistry


class SandboxRunLedger:
    """Owns every read and write of the `sandbox_runs` table."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        mgmt_keys: MgmtKeyStore,
    ) -> None:
        self.store = store
        self.registry = registry
        self.backend = backend
        self.mgmt_keys = mgmt_keys

    # ---------- reconcile (box filesystem -> table) ----------

    def reconcile_live(self) -> int:
        """Daemon sweep: refresh receipts for every running sandbox.

        Rows without runs cost one cheap remote listing (missing .runs dir is
        an empty answer). Returns how many rows answered.
        """
        reconciled = 0
        for row in self.registry.list_running_rows():
            try:
                if self.reconcile_row(row=row):
                    reconciled += 1
            except Exception:  # noqa: BLE001 — the reaper loop must never die
                continue
        return reconciled

    def reconcile_row(self, *, row: dict[str, Any]) -> bool:
        """Refresh records for one sandbox row from its .runs listing.

        A None listing ("no news": dead channel, unsupported backend) never
        mutates records — a flaky read cannot un-finish or lose a run. Only
        live sandboxes are asked; box death leaves the last mirror standing.
        """
        if row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return False
        sandbox_uid = str(row.get("sandbox_uid") or "")
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_uid or not sandbox_id:
            return False
        try:
            listing = self.backend.read_runs(
                sandbox_id=sandbox_id,
                workdir=str(row.get("workdir") or ""),
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                ssh_user=str(row.get("ssh_user") or ""),
                key_path=str(self.mgmt_keys.key_path(sandbox_uid=sandbox_uid)),
            )
        except Exception:  # noqa: BLE001 — observation is best-effort
            return False
        if listing is None:
            return False
        if listing:
            self._record(row=row, listing=listing)
        return True

    def _record(
        self, *, row: dict[str, Any], listing: list[dict[str, Any]]
    ) -> None:
        sandbox_uid = str(row.get("sandbox_uid") or "")
        now = now_iso()
        with self.store.transaction() as conn:
            for run in listing:
                label = str(run.get("label") or "")
                if not label:
                    continue
                exit_code = run.get("exit_code")
                existing = conn.execute(
                    "SELECT exit_code, finished_event_emitted FROM sandbox_runs "
                    "WHERE sandbox_uid = ? AND label = ?",
                    (sandbox_uid, label),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO sandbox_runs (
                          sandbox_uid, label, command, pid, exit_code,
                          started_at, finished_at, first_seen_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sandbox_uid,
                            label,
                            str(run.get("command") or ""),
                            run.get("pid"),
                            exit_code,
                            str(run.get("started_at") or ""),
                            str(run.get("finished_at") or ""),
                            now,
                            now,
                        ),
                    )
                elif existing["exit_code"] is None:
                    # A finished record never regresses; a running one only
                    # needs its terminal facts once they appear.
                    conn.execute(
                        """
                        UPDATE sandbox_runs
                        SET command = ?, pid = ?, exit_code = ?, finished_at = ?,
                            updated_at = ?
                        WHERE sandbox_uid = ? AND label = ?
                        """,
                        (
                            str(run.get("command") or ""),
                            run.get("pid"),
                            exit_code,
                            str(run.get("finished_at") or ""),
                            now,
                            sandbox_uid,
                            label,
                        ),
                    )
                already_emitted = existing is not None and bool(
                    existing["finished_event_emitted"]
                )
                if exit_code is None or already_emitted:
                    continue
                conn.execute(
                    "UPDATE sandbox_runs SET finished_event_emitted = 1 "
                    "WHERE sandbox_uid = ? AND label = ?",
                    (sandbox_uid, label),
                )
                self.store.record_event(
                    conn=conn,
                    project_id=str(row.get("project_id") or ""),
                    event_type="run.finished",
                    target_type="sandbox",
                    target_id=str(row.get("experiment_id") or sandbox_uid),
                    payload={
                        "sandbox_uid": sandbox_uid,
                        "label": label,
                        "exit_code": int(exit_code),
                        "finished_at": str(run.get("finished_at") or ""),
                    },
                )

    # ---------- reads ----------

    def records_for_sandbox(self, *, sandbox_uid: str) -> list[dict[str, Any]]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                """
                SELECT r.*, s.status AS sandbox_status
                FROM sandbox_runs r
                JOIN sandboxes s ON s.sandbox_uid = r.sandbox_uid
                WHERE r.sandbox_uid = ?
                ORDER BY r.first_seen_at, r.label
                """,
                (sandbox_uid,),
            ).fetchall()
            return [row_to_dict(row=item) or {} for item in rows]
        finally:
            conn.close()

    def records_for_experiment(self, *, experiment_id: str) -> list[dict[str, Any]]:
        """Runs across every sandbox ever attached to the experiment.

        Includes detached and terminated sandboxes on purpose: this is the
        "check back after the session (or the box) ended" read.
        """
        conn = self.store.connect()
        try:
            rows = conn.execute(
                """
                SELECT r.*, s.status AS sandbox_status
                FROM sandbox_runs r
                JOIN sandboxes s ON s.sandbox_uid = r.sandbox_uid
                WHERE r.sandbox_uid IN (
                  SELECT DISTINCT sandbox_uid FROM sandbox_attachments
                  WHERE experiment_id = ?
                )
                ORDER BY r.first_seen_at, r.label
                """,
                (experiment_id,),
            ).fetchall()
            return [row_to_dict(row=item) or {} for item in rows]
        finally:
            conn.close()

    # ---------- views ----------

    def nudge_line(self, *, sandbox_uid: str) -> str | None:
        """One compact live-runs line for sandbox.* responses, or None.

        Reads only the mirror (refreshed every daemon sweep) — attaching the
        nudge must never add a remote round-trip to an unrelated tool call.
        """
        records = self.records_for_sandbox(sandbox_uid=sandbox_uid)
        if not records:
            return None
        now = datetime.now(tz=UTC)
        live = [r for r in records if _run_status(r) == "running"]
        finished = [r for r in records if _run_status(r) == "finished"]
        lost = [r for r in records if _run_status(r) == "lost"]
        parts: list[str] = []
        if live:
            shown = ", ".join(
                f"{r.get('label')} {_age(r.get('started_at'), now)}" for r in live[:3]
            )
            more = f", +{len(live) - 3} more" if len(live) > 3 else ""
            parts.append(f"{len(live)} live ({shown}{more})")
        if finished:
            shown = ", ".join(
                f"{r.get('label')}, exit {r.get('exit_code')}" for r in finished[:3]
            )
            more = f", +{len(finished) - 3} more" if len(finished) > 3 else ""
            parts.append(f"{len(finished)} finished ({shown}{more})")
        if lost:
            parts.append(f"{len(lost)} lost with the box")
        return "runs: " + " · ".join(parts) + " — sandbox.runs for detail"


def run_records_view(
    *,
    records: list[dict[str, Any]],
    experiment_id: str = "",
    sandbox_uid: str = "",
) -> dict[str, Any]:
    """Compact sandbox.runs response (<100 tokens typical).

    Per run: label, status, exit_code (finished only), started_at/finished_at,
    log path (experiment_dir-relative). sandbox_uid appears per run only when
    the experiment scope spans more than one sandbox.
    """
    multi_sandbox = len({str(r.get("sandbox_uid") or "") for r in records}) > 1
    runs: list[dict[str, Any]] = []
    live = finished = lost = 0
    for record in records:
        status = _run_status(record)
        view: dict[str, Any] = {
            "label": record.get("label"),
            "status": status,
            "started_at": record.get("started_at") or None,
            "log": f".runs/{record.get('label')}/log.txt",
        }
        if status == "running":
            live += 1
        elif status == "finished":
            finished += 1
            view["exit_code"] = record.get("exit_code")
            view["finished_at"] = record.get("finished_at") or None
        else:
            lost += 1
        if multi_sandbox:
            view["sandbox_uid"] = record.get("sandbox_uid")
        runs.append(view)
    out: dict[str, Any] = {}
    if experiment_id:
        out["experiment_id"] = experiment_id
    if sandbox_uid:
        out["sandbox_uid"] = sandbox_uid
    out.update({"runs": runs, "live": live, "finished": finished})
    if lost:
        out["lost"] = lost
    if not runs:
        out["hint"] = (
            "No merv_run receipts. Launch anything long with "
            "`merv_run <label> -- <command>` on the sandbox: it survives SSH "
            "disconnects and reports its exit code here."
        )
    return out


def _run_status(record: dict[str, Any]) -> str:
    """finished (sentinel present), running (box alive), lost (box died)."""
    if record.get("exit_code") is not None:
        return "finished"
    if record.get("sandbox_status") in ACTIVE_SANDBOX_STATUSES:
        return "running"
    return "lost"


def _age(started_at: Any, now: datetime) -> str:
    started = parse_iso(started_at)
    if started is None:
        return "?"
    seconds = max(int((now - started).total_seconds()), 0)
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"
