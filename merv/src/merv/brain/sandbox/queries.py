"""Typed Sandbox query handler and read projections."""

from __future__ import annotations

import time
from typing import Any

from ..kernel.utils import NotFoundError, ValidationError
from .handler import SandboxHandler
from .messages import (
    GetSandboxQuery,
    ListSandboxesQuery,
    SandboxOptionsQuery,
    SandboxRunsQuery,
    SandboxTerminalQuery,
)
from .sandbox_backend import TranscriptTail
from .sandbox_runs import run_records_view
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    RUNS_WAIT_CAP_SECONDS,
    parse_terminal_markers,
    parse_terminal_snapshot,
)


class SandboxQueryHandler(SandboxHandler):
    def execute_get(self, query: GetSandboxQuery) -> dict[str, Any]:
        experiment_id = query.experiment_id
        project_id = query.project_id
        tenant_id = query.tenant_id
        sandbox_uid = query.sandbox_uid
        include_data_plane_enrichment = query.include_data_plane_enrichment
        experiment_id = (experiment_id or "").strip()
        if not experiment_id and (not (sandbox_uid or "").strip()):
            raise ValidationError("sandbox.get requires experiment_id or sandbox_uid")
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id,
                project_id=project_id,
                tenant_id=tenant_id,
                sandbox_uid=sandbox_uid,
            )
        except NotFoundError:
            if (sandbox_uid or "").strip():
                raise
            if experiment_id and self.registry.exists(experiment_id=experiment_id):
                raise
            return {
                "experiment_id": experiment_id,
                "status": "none",
                "hint": "No sandbox for this experiment — call sandbox.request to create one.",
            }
        row = self.lifecycle.reconcile(row=row)
        self._deliver_secrets_once(row=row, experiment_id=experiment_id)
        return self._agent_result(
            row=row,
            reused=None,
            include_data_plane_enrichment=include_data_plane_enrichment,
            use_sandbox_uid_command=True,
        )

    def execute_options(self, query: SandboxOptionsQuery) -> dict[str, Any]:
        gpu, region = (query.gpu, query.region)
        caps = self.backend.capabilities
        catalog = self._hardware_catalog(gpu=gpu, region=region)
        selection_required = bool(caps.requires_hardware_selection)
        hint = (
            "Pick one options[].instance_type and call sandbox.request(instance_type=..., region=?). Include experiment_id only when attaching the sandbox to an experiment. Options are sorted cheapest-first and reflect live capacity."
            if selection_required
            else "Call sandbox.request(gpu=?, cpu=?, memory=?). Include experiment_id only when attaching the sandbox to an experiment. Omit gpu for a CPU-only sandbox."
        )
        return {"backend": caps.name, **catalog, "hint": hint}

    def execute_list(self, query: ListSandboxesQuery) -> dict[str, Any]:
        return {
            "sandboxes": [
                self._agent_summary(row=row)
                for row in self.repository.list_rows(project_id=query.project_id)
            ]
        }

    def execute_terminal(self, query: SandboxTerminalQuery) -> dict[str, Any]:
        experiment_id = query.experiment_id
        project_id = query.project_id
        sandbox_uid = query.sandbox_uid
        tail = query.tail
        since = query.since
        experiment_id = (experiment_id or "").strip()
        if not experiment_id and (not (sandbox_uid or "").strip()):
            raise ValidationError(
                "sandbox.terminal requires experiment_id or sandbox_uid"
            )
        row = self.registry.fetch_scoped(
            experiment_id=experiment_id, project_id=project_id, sandbox_uid=sandbox_uid
        )
        status = str(row.get("status", "none"))
        sandbox_id = str(row.get("sandbox_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        resolved_experiment_id = experiment_id or str(row.get("experiment_id") or "")
        transcript_key = sandbox_uid or resolved_experiment_id
        unavailable = False

        def _read_for(key: str) -> TranscriptTail:
            return self.backend.read_transcript(
                sandbox_id=sandbox_id,
                experiment_id=key,
                volume_name=str(row.get("volume_name") or ""),
                workdir=str(row.get("workdir") or ""),
                tail=None,
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                ssh_user=str(row.get("ssh_user") or ""),
                key_path=str(self._mgmt_key_path(row=row)),
            )

        def _read() -> TranscriptTail:
            window = _read_for(transcript_key)
            if (
                window.data
                or window.total_bytes
                or (not resolved_experiment_id)
                or (resolved_experiment_id == transcript_key)
            ):
                return window
            return _read_for(resolved_experiment_id)

        window = TranscriptTail(data=b"", total_bytes=0)
        try:
            window = self.transcript_cache.get_or_read(
                sandbox_id=sandbox_id, read=_read, since=since
            )
        except Exception as exc:
            full = f"(terminal unavailable: {exc})"
            unavailable = True
        if unavailable:
            transcript = full
            cursor = len(full)
        else:
            cursor = window.total_bytes
            window_start = max(cursor - len(window.data), 0)
            if since is not None:
                start = min(max(int(since) - window_start, 0), len(window.data))
                raw = window.data[start:]
            elif tail is not None and tail >= 0 and (len(window.data) > tail):
                raw = window.data[-tail:]
            else:
                raw = window.data
            transcript = raw.decode("utf-8", errors="replace")
            full = window.data.decode("utf-8", errors="replace")
        last_command: dict[str, Any] | None = None
        command_status_stale = False
        if unavailable:
            last_command = self.registry.command_snapshot(row=row)
            command_status_stale = last_command is not None
            last_exit_code = (
                None if last_command is None else last_command.get("exit_code")
            )
            last_command_finished_at = (
                None if last_command is None else last_command.get("finished_at")
            )
            command_running = (
                None
                if last_command is None
                else last_command.get("status") == "running"
                and status in ACTIVE_SANDBOX_STATUSES
            )
        else:
            snapshot = parse_terminal_snapshot(full)
            if (
                snapshot.get("status") == "running"
                and status not in ACTIVE_SANDBOX_STATUSES
            ):
                snapshot = {**snapshot, "status": "interrupted"}
            last_command = (
                self.registry.record_command_snapshot(
                    sandbox_uid=sandbox_uid, snapshot=snapshot
                )
                if snapshot.get("command_id")
                else None
            )
            last_exit_code, last_command_finished_at, in_flight = (
                parse_terminal_markers(full)
            )
            command_running = in_flight and status in ACTIVE_SANDBOX_STATUSES
        return self._with_runs_nudge(
            view={
                "experiment_id": resolved_experiment_id,
                "sandbox_uid": sandbox_uid,
                "sandbox_id": row.get("sandbox_id", ""),
                "status": status,
                "running": status in ACTIVE_SANDBOX_STATUSES,
                "transcript": transcript,
                "cursor": cursor,
                "new_chars": len(transcript) if since is not None else None,
                "last_exit_code": last_exit_code,
                "last_command_finished_at": last_command_finished_at,
                "command_running": command_running,
                "last_command": last_command,
                "command_status_stale": command_status_stale,
            },
            sandbox_uid=sandbox_uid,
        )

    def execute_runs(self, query: SandboxRunsQuery) -> dict[str, Any]:
        experiment_id = query.experiment_id
        project_id = query.project_id
        tenant_id = query.tenant_id
        sandbox_uid = query.sandbox_uid
        wait_seconds = query.wait_seconds
        experiment_id = (experiment_id or "").strip()
        sandbox_uid = (sandbox_uid or "").strip()
        if not experiment_id and (not sandbox_uid):
            raise ValidationError("sandbox.runs requires experiment_id or sandbox_uid")
        try:
            self.registry.fetch_scoped(
                experiment_id=experiment_id,
                project_id=project_id,
                tenant_id=tenant_id,
                sandbox_uid=sandbox_uid or None,
            )
        except NotFoundError:
            if sandbox_uid:
                raise
        wait = min(max(float(wait_seconds or 0), 0.0), RUNS_WAIT_CAP_SECONDS)
        deadline = time.monotonic() + wait
        baseline_finished: set[tuple[str, str]] | None = None
        while True:
            self._reconcile_runs_targets(
                experiment_id=experiment_id, sandbox_uid=sandbox_uid
            )
            records = (
                self.runs_ledger.records_for_sandbox(sandbox_uid=sandbox_uid)
                if sandbox_uid
                else self.runs_ledger.records_for_experiment(
                    experiment_id=experiment_id
                )
            )
            finished_now = {
                (str(r.get("sandbox_uid") or ""), str(r.get("label") or ""))
                for r in records
                if r.get("exit_code") is not None
            }
            if baseline_finished is None:
                baseline_finished = finished_now
            still_running = any((r.get("exit_code") is None for r in records))
            if (
                finished_now - baseline_finished
                or not still_running
                or time.monotonic() >= deadline
            ):
                return run_records_view(
                    records=records,
                    experiment_id=experiment_id,
                    sandbox_uid=sandbox_uid,
                )
            time.sleep(
                min(self.runs_wait_poll_seconds, max(deadline - time.monotonic(), 0.1))
            )

    def _reconcile_runs_targets(self, *, experiment_id: str, sandbox_uid: str) -> None:
        if sandbox_uid:
            try:
                rows = [self.registry.get_by_uid(sandbox_uid=sandbox_uid)]
            except NotFoundError:
                rows = []
        else:
            rows = self.registry.list_by_experiment(experiment_id=experiment_id)
        for row in rows:
            self.runs_ledger.reconcile_row(row=row)

    def health(self) -> dict[str, Any]:
        health = self.backend.health()
        result = {"ok": bool(health.get("ok"))}
        if not result["ok"] and health.get("error"):
            result["error"] = health["error"]
        return result

    def get_row(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id or "",
                project_id=project_id,
                sandbox_uid=sandbox_uid,
            )
        except NotFoundError:
            return None
        return self.lifecycle.reconcile(row=row)

    def rows(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        return self.registry.list_rows(project_id=project_id)

    def row_view(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return self._row_view(row=row)

    def backend_health(self) -> dict[str, Any]:
        return dict(self.backend.health())

    def sample_metrics(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        return self.metrics.sample_metrics(
            experiment_id=experiment_id, project_id=project_id, sandbox_uid=sandbox_uid
        )

    def sandboxes_for_experiment(
        self, *, conn, experiment_id: str
    ) -> list[dict[str, Any]]:
        rows = self.repository.rows_for_experiment(
            conn=conn, experiment_id=experiment_id
        )
        return [self._row_view(row=row, conn=conn) for row in rows]

    def sandboxes_for_project(self, *, conn, project_id: str) -> list[dict[str, Any]]:
        rows = self.repository.rows_for_project(conn=conn, project_id=project_id)
        return [self._row_view(row=row, conn=conn) for row in rows]


__all__ = ["SandboxQueryHandler"]
