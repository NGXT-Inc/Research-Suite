"""Research-state adapter for execution jobs."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ..state.activity import ActivityLogger
from ..utils import NotFoundError, PermissionDeniedError, ValidationError
from ..utils import new_id
from ..execution import (
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    ExecutionProgress,
    ExecutionBackend,
    JobExecutionPolicy,
    ProgressCallback,
    SubmitStatusReport,
    TERMINAL_STATUSES,
)
from ..state.store import StateStore, row_to_dict
from ..utils import now_iso


# Was 300s; in practice Modal cold-start + first-sync against a populated
# volume regularly exceeds 5 min, so the previous default falsely killed
# perfectly healthy submissions before runtime_job_id was published. With
# per-stage progress events (Phase 3) the stale check now also rests on
# progress_updated_at, so the only paths this guards are genuinely-stuck
# submits — 900s is the conservative "this is really stuck" window.
DEFAULT_MODAL_SUBMIT_STALE_SECONDS = 900
MODAL_SUBMIT_STALE_ENV = "RESEARCH_PLUGIN_MODAL_SUBMIT_STALE_SECONDS"
SUBMIT_OWNER_PID_KEY = "submit_owner_pid"
SUBMIT_WORKER_NAME_KEY = "submit_worker_name"


def compose_nested_status(
    *,
    status: str,
    progress_phase: str | None,
    live_report: SubmitStatusReport | None = None,
) -> str:
    """Combine a job's top-level `status` with a finer-grained substate.

    Sources, in priority order:
      1. A live submission pipeline report (Modal submit in flight) — fresher
         than the DB and not subject to inter-stage races.
      2. The DB-stored `progress_phase` column.

    Returns "{status}.{substate}" when a substate adds information, otherwise
    just "{status}". Terminal states (succeeded/failed/cancelled) intentionally
    drop the suffix — once a job is done, the substate is no longer useful.
    """
    if status in TERMINAL_STATUSES:
        return status
    if status == "submitting" and live_report is not None and live_report.current:
        return f"submitting.{live_report.current}"
    if progress_phase and progress_phase != status:
        return f"{status}.{progress_phase}"
    return status


class JobService:
    """Owns job persistence and delegates execution to a backend."""

    def __init__(
        self,
        *,
        store: StateStore,
        execution_backend: ExecutionBackend,
        activity: ActivityLogger | None = None,
    ) -> None:
        self.store = store
        self.backend = execution_backend
        self.policy = JobExecutionPolicy(repo_root=store.repo_root)
        # Optional — when set, exceptions in background submit / reconcile
        # paths are logged with full traceback to .research_plugin/activity.jsonl,
        # which is the only place HTTP and MCP processes share a view of failures.
        self.activity = activity

    def submit(
        self,
        *,
        experiment_id: str,
        command: str,
        cwd: str = ".",
        expected_outputs: list[str] | None = None,
        env: dict[str, str] | None = None,
        backend_hints: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        env, backend_hints = self._normalise_submit_inputs(env=env, backend_hints=backend_hints)
        with self._translate_runtime_errors():
            spec = self.policy.validate(
                command=command,
                cwd=cwd,
                expected_outputs=expected_outputs,
                env=env,
                backend_hints=backend_hints,
            )

        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            experiment = conn.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if experiment is None:
                raise NotFoundError(f"experiment not found: {experiment_id}")
            if experiment["project_id"] != project_id:
                raise NotFoundError(
                    f"experiment not found in project {project_id}: {experiment_id}"
                )
            if experiment["status"] not in {"ready_to_run", "running"}:
                raise PermissionDeniedError(
                    "job.submit requires experiment status ready_to_run or running"
                )
            job_id = new_id(prefix="job")
            now = now_iso()
            metadata = {
                "research_plugin_job_id": job_id,
                "experiment_id": experiment_id,
                "project_id": project_id,
            }
            if self._submits_asynchronously:
                metadata.update(
                    {
                        SUBMIT_OWNER_PID_KEY: str(os.getpid()),
                        SUBMIT_WORKER_NAME_KEY: _submit_worker_name(job_id=job_id),
                    }
                )
            spec = replace(spec, metadata=metadata)
            conn.execute(
                """
                INSERT INTO jobs (
                  id, project_id, experiment_id, attempt_index, backend,
                  command, cwd, expected_outputs_json, backend_hints_json,
                  metadata_json, status, progress_phase, progress_message,
                  progress_updated_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitting', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    project_id,
                    experiment_id,
                    int(experiment["attempt_index"]),
                    self.backend.capabilities.name,
                    spec.command,
                    spec.cwd,
                    json.dumps(list(spec.expected_outputs)),
                    json.dumps(dict(spec.backend_hints), sort_keys=True),
                    json.dumps(dict(spec.metadata), sort_keys=True),
                    "accepted",
                    "Submission accepted",
                    now,
                    now,
                    now,
                ),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="job.created",
                target_type="job",
                target_id=job_id,
                payload={
                    "experiment_id": experiment_id,
                    "attempt_index": int(experiment["attempt_index"]),
                    "backend": self.backend.capabilities.name,
                },
            )
            if self._submits_asynchronously:
                conn.execute(
                    "UPDATE experiments SET status = 'running', updated_at = ? WHERE id = ?",
                    (now, experiment_id),
                )

        if self._submits_asynchronously:
            self._start_submit_worker(
                job_id=job_id,
                project_id=project_id,
                experiment_id=experiment_id,
                spec=spec,
            )
            return self.get_status(job_id=job_id, project_id=project_id, reconcile=False)

        return self._submit_now(
            job_id=job_id,
            project_id=project_id,
            experiment_id=experiment_id,
            spec=spec,
        )

    def _submit_now(
        self,
        *,
        job_id: str,
        project_id: str,
        experiment_id: str,
        spec,
    ) -> dict[str, Any]:
        try:
            runtime_job_id = self.backend.submit(
                spec=spec,
                progress=self._progress_callback(job_id=job_id),
            )
        except Exception as exc:  # noqa: BLE001
            self._log_submit_exception(
                exc=exc,
                job_id=job_id,
                project_id=project_id,
                experiment_id=experiment_id,
            )
            with self.store.transaction() as conn:
                self._mark_submit_failed(
                    conn=conn,
                    job_id=job_id,
                    project_id=project_id,
                    error=_format_exc(exc),
                )
                return self.get_status(job_id=job_id, conn=conn, reconcile=False)

        self._record_submit_success(
            job_id=job_id,
            project_id=project_id,
            experiment_id=experiment_id,
            runtime_job_id=runtime_job_id,
        )
        return self.get_status(job_id=job_id, project_id=project_id, reconcile=False)

    def _start_submit_worker(
        self,
        *,
        job_id: str,
        project_id: str,
        experiment_id: str,
        spec,
    ) -> None:
        thread = threading.Thread(
            target=self._run_submit_worker,
            kwargs={
                "job_id": job_id,
                "project_id": project_id,
                "experiment_id": experiment_id,
                "spec": spec,
            },
            name=_submit_worker_name(job_id=job_id),
            daemon=False,
        )
        thread.start()

    def _run_submit_worker(
        self,
        *,
        job_id: str,
        project_id: str,
        experiment_id: str,
        spec,
    ) -> None:
        try:
            runtime_job_id = self.backend.submit(
                spec=spec,
                progress=self._progress_callback(job_id=job_id),
            )
        except Exception as exc:  # noqa: BLE001
            # Background-thread failures otherwise disappear into a stringified
            # DB error column with no traceback. Log to activity.jsonl first so
            # both HTTP and MCP processes can see the full call site.
            self._log_submit_exception(
                exc=exc,
                job_id=job_id,
                project_id=project_id,
                experiment_id=experiment_id,
            )
            with self.store.transaction() as conn:
                row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is not None and row["status"] == "submitting":
                    self._mark_submit_failed(
                        conn=conn,
                        job_id=job_id,
                        project_id=project_id,
                        error=_format_exc(exc),
                    )
            return

        self._record_submit_success(
            job_id=job_id,
            project_id=project_id,
            experiment_id=experiment_id,
            runtime_job_id=runtime_job_id,
        )

    def _record_submit_success(
        self,
        *,
        job_id: str,
        project_id: str,
        experiment_id: str,
        runtime_job_id: str,
    ) -> None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] != "submitting":
                return
            now = now_iso()
            self._update_job(
                conn=conn,
                job_id=job_id,
                runtime_job_id=runtime_job_id,
                status="queued",
                progress_phase="queued",
                progress_message="Accepted by execution backend",
                progress_updated_at=now,
                submitted_at=now,
                updated_at=now,
            )
            conn.execute(
                "UPDATE experiments SET status = 'running', updated_at = ? WHERE id = ?",
                (now, experiment_id),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="job.submitted",
                target_type="job",
                target_id=job_id,
                payload={"runtime_job_id": runtime_job_id, "backend": self.backend.capabilities.name},
            )

    def _log_submit_exception(
        self,
        *,
        exc: BaseException,
        job_id: str,
        project_id: str,
        experiment_id: str,
    ) -> None:
        """Cross-process traceback capture for submit-time failures.

        Both the HTTP server and the MCP server append to the same
        .research_plugin/activity.jsonl, so this is the only way for the two
        processes to share a view of a failure. Without it, the DB only
        records `str(exc)` — which is empty information for opaque gRPC
        strings like "No item with that key".
        """
        if self.activity is None:
            return
        self.activity.exception(
            event_type="job.submit.exception",
            payload={
                "job_id": job_id,
                "project_id": project_id,
                "experiment_id": experiment_id,
            },
            exc=exc,
        )

    @property
    def _submits_asynchronously(self) -> bool:
        return self.backend.capabilities.name == "modal"

    def _normalise_submit_inputs(
        self,
        *,
        env: dict[str, str] | None,
        backend_hints: dict[str, Any] | None,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        clean_env = dict(env or {})
        clean_hints = dict(backend_hints or {})
        misplaced_env = clean_hints.pop("env", None)
        if misplaced_env is not None:
            if not isinstance(misplaced_env, dict):
                raise ValidationError("backend_hints.env must be an object; use top-level env")
            for key, value in misplaced_env.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValidationError("backend_hints.env must contain string keys and values")
                clean_env.setdefault(key, value)
        # Free-form notes are useful conversation context, but they are not
        # backend configuration and should not make a submission fail.
        clean_hints.pop("note", None)
        return clean_env, clean_hints

    def _progress_callback(self, *, job_id: str) -> ProgressCallback:
        def report(progress: ExecutionProgress) -> None:
            phase = str(progress.phase or "").strip()[:80]
            message = str(progress.message or "").strip()[:300]
            runtime_job_id = str(progress.runtime_job_id or "").strip()
            metadata = dict(progress.metadata or {})
            if not phase and not message and not runtime_job_id and not metadata:
                return
            with self.store.transaction() as conn:
                # Select runtime_job_id too — line 377 reads row["runtime_job_id"]
                # to decide whether this progress callback should commit one,
                # and sqlite3.Row raises IndexError("No item with that key") if
                # the column wasn't in the SELECT. That bug was previously
                # surfacing as an opaque "No item with that key" submit error
                # for any Modal job that produced a runtime_job_id via progress.
                row = conn.execute(
                    "SELECT status, runtime_job_id FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if row is None or row["status"] != "submitting":
                    return
                now = now_iso()
                updates: dict[str, Any] = {
                    "progress_updated_at": now,
                    "updated_at": now,
                }
                if phase:
                    updates["progress_phase"] = phase
                if message:
                    updates["progress_message"] = message
                if runtime_job_id and not row["runtime_job_id"]:
                    updates["runtime_job_id"] = runtime_job_id
                    updates["submitted_at"] = now
                for column in ("sandbox_id", "gpu", "ssh_address"):
                    value = str(metadata.get(column, "") or "").strip()[:255]
                    if value:
                        updates[column] = value
                self._update_job(conn=conn, job_id=job_id, **updates)

        return report

    def get_status(
        self,
        *,
        job_id: str,
        project_id: str | None = None,
        conn=None,
        reconcile: bool = True,
    ) -> dict[str, Any]:
        return self._get_job(
            job_id=job_id,
            project_id=project_id,
            conn=conn,
            reconcile=reconcile,
            agent=True,
        )

    def get_status_for_ui(
        self,
        *,
        job_id: str,
        project_id: str | None = None,
        reconcile: bool = True,
    ) -> dict[str, Any]:
        return self._get_job(
            job_id=job_id,
            project_id=project_id,
            conn=None,
            reconcile=reconcile,
            agent=False,
        )

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        experiment_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        return {
            "jobs": [
                self._job_summary(job=job)
                for job in self._list_jobs_internal(
                    project_id=project_id,
                    experiment_id=experiment_id,
                    status=status,
                )
            ]
        }

    def list_jobs_for_ui(
        self,
        *,
        project_id: str | None = None,
        experiment_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        return {
            "jobs": self._list_jobs_internal(
                project_id=project_id,
                experiment_id=experiment_id,
                status=status,
            )
        }

    def logs(
        self, *, job_id: str, tail: int | None = None, project_id: str | None = None
    ) -> dict[str, Any]:
        # Verify project ownership before reconcile, which polls the backend and
        # may materialize outputs — a wrong-project caller must not trigger those
        # side effects.
        self._fetch_job_row(job_id=job_id, project_id=project_id)
        self.reconcile(job_id=job_id)
        row = self._fetch_job_row(job_id=job_id, project_id=project_id)
        logs = row["logs_cache"] or ""
        if row["runtime_job_id"]:
            try:
                logs = self.backend.logs(runtime_job_id=row["runtime_job_id"])
                with self.store.transaction() as conn:
                    self._update_job(
                        conn=conn, job_id=job_id, logs_cache=logs, updated_at=now_iso()
                    )
            except Exception as exc:  # noqa: BLE001
                logs = (row["logs_cache"] or "") or f"Unable to fetch job logs: {exc}"
        lines = logs.splitlines()
        if tail is not None:
            tail_n = max(0, int(tail))
            lines = lines[len(lines) - tail_n :] if tail_n else []
        return {"logs": "\n".join(lines)}

    def cancel(self, *, job_id: str, project_id: str | None = None) -> dict[str, Any]:
        row = self._fetch_job_row(job_id=job_id, project_id=project_id)
        stopped = False
        if row["runtime_job_id"] and row["status"] not in TERMINAL_STATUSES:
            stopped = self.backend.cancel(runtime_job_id=row["runtime_job_id"])
        with self.store.transaction() as conn:
            now = now_iso()
            self._update_job(
                conn=conn,
                job_id=job_id,
                status="cancelled",
                progress_phase="cancelled",
                progress_message="Cancelled",
                progress_updated_at=now,
                finished_at=now,
                updated_at=now,
            )
            self.store.record_event(
                conn=conn,
                project_id=row["project_id"],
                event_type="job.cancelled",
                target_type="job",
                target_id=job_id,
                payload={"stopped": stopped},
            )
            return self.get_status(job_id=job_id, conn=conn, reconcile=False)

    def health(self) -> dict[str, Any]:
        health = self.backend.health()
        result = {"ok": bool(health.get("ok"))}
        if not result["ok"] and health.get("error"):
            result["error"] = health["error"]
        return result

    def health_for_ui(self) -> dict[str, Any]:
        return self.backend.health()

    def reconcile(self, *, job_id: str) -> dict[str, Any]:
        job = self._load_job_for_ui(job_id=job_id)
        if self._recover_runtime_job_id_if_possible(job=job):
            job = self._load_job_for_ui(job_id=job_id)
        if self._mark_stale_submission_if_needed(job=job):
            job = self._load_job_for_ui(job_id=job_id)
        if job.get("runtime_job_id") and job["status"] not in TERMINAL_STATUSES:
            self._reconcile_backend_status(job=job)
            job = self._load_job_for_ui(job_id=job_id)
        if job["status"] == "succeeded" and not job.get("materialized_at"):
            self._materialize_outputs(job=job)
            job = self._load_job_for_ui(job_id=job_id)
        return job

    def _reconcile_backend_status(self, *, job: dict[str, Any]) -> None:
        backend_status = None
        backend_error: str | None = None
        logs_cache: str | None = None
        try:
            backend_status = self.backend.status(runtime_job_id=job["runtime_job_id"])
            if backend_status.state in TERMINAL_STATUSES and not job.get("finished_at"):
                try:
                    logs_cache = self.backend.logs(runtime_job_id=job["runtime_job_id"])
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            if self.activity is not None:
                self.activity.exception(
                    event_type="job.reconcile.exception",
                    payload={
                        "job_id": job.get("id", ""),
                        "project_id": job.get("project_id", ""),
                        "experiment_id": job.get("experiment_id", ""),
                        "runtime_job_id": job.get("runtime_job_id", ""),
                    },
                    exc=exc,
                )
            backend_error = _format_exc(exc)

        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job['id']}")
            current = self._hydrate_for_ui(row=row)
            if current["status"] in TERMINAL_STATUSES:
                return
            if backend_error:
                self._update_job(
                    conn=conn,
                    job_id=job["id"],
                    error=backend_error,
                    updated_at=now_iso(),
                )
                return
            if backend_status is None:
                return

            now = now_iso()
            # Prefer the backend's finer-grained `phase` when supplied
            # (e.g. Modal's "waiting_sandbox" / "runner_starting" substates
            # of queued); fall back to the state name when the backend
            # doesn't have anything more specific to say.
            updates: dict[str, Any] = {
                "status": backend_status.state,
                "progress_phase": backend_status.phase or backend_status.state,
                "progress_message": self._progress_message_for_state(backend_status.state),
                "progress_updated_at": now,
                "updated_at": now,
            }
            # Persist a job-level error only for terminal failures. Operational
            # notes the backend attaches to running/queued statuses (e.g. a
            # transient "status read timed out" while a slow control plane is
            # briefly unreadable) must not stick: clear the column so a later
            # good poll or the committed volume status wins and the UI shows
            # logs instead of a stale error banner.
            if backend_status.state in TERMINAL_STATUSES:
                updates["error"] = backend_status.error or None
            else:
                updates["error"] = None
            if backend_status.started_at:
                updates["started_at"] = backend_status.started_at
            elif backend_status.state == "running" and not current.get("started_at"):
                updates["started_at"] = now_iso()
            if backend_status.finished_at:
                updates["finished_at"] = backend_status.finished_at
            elif backend_status.state in TERMINAL_STATUSES and not current.get("finished_at"):
                updates["finished_at"] = now
            if logs_cache is not None:
                updates["logs_cache"] = logs_cache
            self._update_job(conn=conn, job_id=job["id"], **updates)
            if backend_status.state in TERMINAL_STATUSES:
                self.store.record_event(
                    conn=conn,
                    project_id=job["project_id"],
                    event_type=f"job.{backend_status.state}",
                    target_type="job",
                    target_id=job["id"],
                    payload={"runtime_job_id": job["runtime_job_id"], "backend": job["backend"]},
                )

    def _mark_stale_submission_if_needed(self, *, job: dict[str, Any]) -> bool:
        if job["status"] != "submitting" or job.get("runtime_job_id"):
            return False
        if str(job.get("backend") or "") != "modal":
            return False

        timeout_seconds = self._modal_submit_stale_seconds()
        if timeout_seconds <= 0:
            return False
        updated_at = (
            job.get("progress_updated_at")
            or job.get("updated_at")
            or job.get("created_at")
        )
        age = _age_seconds(updated_at)
        if age is None or age < timeout_seconds:
            return False

        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job['id']}")
            current = self._hydrate_for_ui(row=row)
            if (
                current["status"] != "submitting"
                or current.get("runtime_job_id")
                or str(current.get("backend") or "") != "modal"
            ):
                return False
            owner_available = _submit_owner_available(job=current)
            if owner_available:
                error = (
                    "Modal submission did not publish a runtime job id within "
                    f"{int(age)} seconds; the submit worker may be blocked in "
                    "Modal sandbox setup. Retry job.submit."
                )
            else:
                error = (
                    "Modal submission did not finish before the submit owner "
                    f"became unavailable after {int(age)} seconds; retry job.submit."
                )
            self._mark_submit_failed(
                conn=conn,
                job_id=job["id"],
                project_id=job["project_id"],
                error=error,
            )
        return True

    def _recover_runtime_job_id_if_possible(self, *, job: dict[str, Any]) -> bool:
        if (
            job["status"] != "submitting"
            or job.get("runtime_job_id")
            or str(job.get("backend") or "") != "modal"
        ):
            return False
        recover = getattr(self.backend, "recover_runtime_job_id", None)
        if not callable(recover):
            return False
        try:
            runtime_job_id = recover(
                job_id=job["id"],
                project_id=job["project_id"],
                experiment_id=job["experiment_id"],
                backend_hints=job.get("backend_hints") or {},
            )
        except Exception as exc:  # noqa: BLE001
            if self.activity is not None:
                self.activity.exception(
                    event_type="job.recover.exception",
                    payload={
                        "job_id": job.get("id", ""),
                        "project_id": job.get("project_id", ""),
                        "experiment_id": job.get("experiment_id", ""),
                    },
                    exc=exc,
                )
            with self.store.transaction() as conn:
                self._update_job(
                    conn=conn,
                    job_id=job["id"],
                    error=f"Unable to recover Modal runtime job id: {_format_exc(exc)}",
                    updated_at=now_iso(),
                )
            return False
        if not runtime_job_id:
            return False
        now = now_iso()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job['id']}")
            current = self._hydrate_for_ui(row=row)
            if current["status"] != "submitting" or current.get("runtime_job_id"):
                return False
            self._update_job(
                conn=conn,
                job_id=job["id"],
                runtime_job_id=runtime_job_id,
                submitted_at=now,
                progress_phase=current.get("progress_phase") or "starting",
                progress_message=current.get("progress_message") or "Recovered Modal sandbox",
                progress_updated_at=now,
                updated_at=now,
            )
        return True

    def jobs_for_experiment(self, *, conn, experiment_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE experiment_id = ? ORDER BY rowid DESC",
            (experiment_id,),
        ).fetchall()
        return [self._hydrate_for_ui(row=row) for row in rows]

    def jobs_for_project(self, *, conn, project_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE project_id = ? ORDER BY rowid DESC",
            (project_id,),
        ).fetchall()
        return [self._hydrate_for_ui(row=row) for row in rows]

    def _list_jobs_internal(
        self,
        *,
        project_id: str | None,
        experiment_id: str | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            clauses: list[str] = ["project_id = ?"]
            params: list[Any] = [project_id]
            if experiment_id:
                clauses.append("experiment_id = ?")
                params.append(experiment_id)
            if status:
                clauses.append("status = ?")
                params.append(status)
            where = "WHERE " + " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY rowid DESC", params
            ).fetchall()
            return [self._hydrate_for_ui(row=row) for row in rows]
        finally:
            conn.close()

    def _get_job(
        self,
        *,
        job_id: str,
        project_id: str | None,
        conn,
        reconcile: bool,
        agent: bool,
    ) -> dict[str, Any]:
        owns_conn = conn is None
        if conn is None:
            conn = self.store.connect()
        try:
            if owns_conn:
                project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            if project_id is not None and row["project_id"] != project_id:
                raise NotFoundError(f"job not found in project {project_id}: {job_id}")
            if reconcile:
                self.reconcile(job_id=job_id)
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            full = self._hydrate_for_ui(row=row)
            return self._hydrate_for_agent(job=full) if agent else full
        finally:
            if owns_conn:
                conn.close()

    def _fetch_job_row(self, *, job_id: str, project_id: str | None) -> dict[str, Any]:
        """Lean read for ops that only need a few columns. Skips JSON parsing
        and filesystem stats. Enforces project scoping when given."""
        conn = self.store.connect()
        try:
            if project_id is not None:
                project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            if project_id is not None and row["project_id"] != project_id:
                raise NotFoundError(f"job not found in project {project_id}: {job_id}")
            return dict(row)
        finally:
            conn.close()

    def _load_job_for_ui(self, *, job_id: str) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            return self._hydrate_for_ui(row=row)
        finally:
            conn.close()

    def _hydrate_for_agent(self, *, job: dict[str, Any]) -> dict[str, Any]:
        result = {
            "id": job["id"],
            "status": job["status"],
            "nested_status": job.get("nested_status", job["status"]),
            "outputs": [{"path": item["path"], "exists": item["exists"]} for item in job["outputs"]],
            "error": job.get("error"),
        }
        if job.get("progress_message"):
            result["message"] = job["progress_message"]
        if job.get("materialize_error"):
            result["warning"] = job["materialize_error"]
        return result

    def _hydrate_for_ui(self, *, row) -> dict[str, Any]:
        data = row_to_dict(row=row) or {}
        expected_outputs = self._loads(value=data.pop("expected_outputs_json", "[]"), default=[])
        data["expected_outputs"] = expected_outputs
        data["backend_hints"] = self._loads(value=data.pop("backend_hints_json", "{}"), default={})
        data["metadata"] = self._loads(value=data.pop("metadata_json", "{}"), default={})
        if data.get("status") == "submitting" and not data.get("progress_message"):
            data["progress_phase"] = data.get("progress_phase") or "accepted"
            data["progress_message"] = "Submission accepted"
        live_report = (
            self._live_submit_report(job_id=str(data.get("id") or ""))
            if data.get("status") == "submitting"
            else None
        )
        data["nested_status"] = compose_nested_status(
            status=str(data.get("status") or ""),
            progress_phase=data.get("progress_phase"),
            live_report=live_report,
        )
        data["outputs"] = self._output_statuses(expected_outputs=expected_outputs)
        return data

    def _job_summary(self, *, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job["id"],
            "status": job["status"],
            "nested_status": job.get("nested_status", job["status"]),
            "experiment_id": job["experiment_id"],
        }

    def _live_submit_report(self, *, job_id: str) -> SubmitStatusReport | None:
        """Ask the backend for the live submit pipeline's status, if any.

        Only Modal exposes this; other backends (Ray, fake) submit
        synchronously and have no in-flight submit to query. Returns None
        when the backend doesn't support it, when there's no submit in
        flight for this job_id, or when the lookup raises (defensive —
        nested_status falls back to the DB phase).
        """
        if not job_id:
            return None
        lookup = getattr(self.backend, "live_submit_status", None)
        if not callable(lookup):
            return None
        try:
            return lookup(job_id=job_id)
        except Exception:  # noqa: BLE001
            return None

    def _output_statuses(self, *, expected_outputs: list[str]) -> list[dict[str, Any]]:
        outputs = []
        for rel_path in expected_outputs:
            path = self.store.repo_root / rel_path
            exists = path.exists()
            outputs.append(
                {
                    "path": rel_path,
                    "exists": exists,
                    "is_file": path.is_file() if exists else False,
                }
            )
        return outputs

    def _materialize_outputs(self, *, job: dict[str, Any]) -> None:
        self._set_progress(
            job_id=job["id"],
            phase="materializing",
            message="Materializing outputs",
            only_status=None,
        )
        # Non-materializing backends already wrote outputs to disk; just
        # stamp the job and skip the round trip.
        if not self.backend.capabilities.materializes_outputs:
            now = now_iso()
            with self.store.transaction() as conn:
                current = self._locked_job_for_materialization(conn=conn, job_id=job["id"])
                if current is None:
                    return
                self._update_job(
                    conn=conn,
                    job_id=job["id"],
                    materialized_at=now,
                    progress_phase="succeeded",
                    progress_message="Finished",
                    progress_updated_at=now,
                    materialize_error=None,
                    updated_at=now,
                )
            return
        max_attempts = self._materialize_retry_limit()
        attempts = int(job.get("materialize_attempts") or 0)
        if attempts >= max_attempts:
            return
        try:
            self.backend.materialize_outputs(
                runtime_job_id=job["runtime_job_id"],
                expected_outputs=job["expected_outputs"],
                repo_root=self.store.repo_root,
            )
        except Exception as exc:  # noqa: BLE001
            with self.store.transaction() as conn:
                current = self._locked_job_for_materialization(conn=conn, job_id=job["id"])
                if current is None:
                    return
                current_attempts = int(current.get("materialize_attempts") or 0)
                if current_attempts >= max_attempts:
                    return
                self._update_job(
                    conn=conn,
                    job_id=job["id"],
                    materialize_attempts=current_attempts + 1,
                    materialize_error=str(exc),
                    progress_phase="materializing",
                    progress_message="Output materialization failed",
                    progress_updated_at=now_iso(),
                    updated_at=now_iso(),
                )
            return
        with self.store.transaction() as conn:
            current = self._locked_job_for_materialization(conn=conn, job_id=job["id"])
            if current is None:
                return
            current_attempts = int(current.get("materialize_attempts") or 0)
            now = now_iso()
            self._update_job(
                conn=conn,
                job_id=job["id"],
                materialized_at=now,
                materialize_attempts=current_attempts + 1,
                materialize_error=None,
                progress_phase="succeeded",
                progress_message="Finished",
                progress_updated_at=now,
                updated_at=now,
            )

    def _locked_job_for_materialization(self, *, conn, job_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"job not found: {job_id}")
        current = self._hydrate_for_ui(row=row)
        if current["status"] != "succeeded" or current.get("materialized_at"):
            return None
        return current

    def _mark_submit_failed(
        self,
        *,
        conn,
        job_id: str,
        project_id: str,
        error: str | None = None,
    ) -> None:
        now = now_iso()
        self._update_job(
            conn=conn,
            job_id=job_id,
            status="failed",
            error=error,
            progress_phase="failed",
            progress_message="Submission failed",
            progress_updated_at=now,
            finished_at=now,
            updated_at=now,
        )
        self.store.record_event(
            conn=conn,
            project_id=project_id,
            event_type="job.submit_failed",
            target_type="job",
            target_id=job_id,
            payload={"error": error or ""},
        )

    def _update_job(self, *, conn, job_id: str, **updates: Any) -> None:
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?", [*updates.values(), job_id]
        )

    def _set_progress(
        self,
        *,
        job_id: str,
        phase: str,
        message: str,
        only_status: str | None = "submitting",
    ) -> None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            if only_status is not None and row["status"] != only_status:
                return
            now = now_iso()
            self._update_job(
                conn=conn,
                job_id=job_id,
                progress_phase=phase,
                progress_message=message,
                progress_updated_at=now,
                updated_at=now,
            )

    def _progress_message_for_state(self, state: str) -> str:
        return {
            "queued": "Queued by execution backend",
            "running": "Running",
            "succeeded": "Execution finished",
            "failed": "Execution failed",
            "cancelled": "Cancelled",
        }.get(state, "")

    def _loads(self, *, value: str, default: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, json.JSONDecodeError):
            return default

    def _materialize_retry_limit(self) -> int:
        raw = os.environ.get("RESEARCH_PLUGIN_MATERIALIZE_RETRIES", "3")
        try:
            return max(0, int(raw))
        except ValueError:
            return 3

    def _modal_submit_stale_seconds(self) -> int:
        raw = os.environ.get(MODAL_SUBMIT_STALE_ENV, str(DEFAULT_MODAL_SUBMIT_STALE_SECONDS))
        try:
            return max(0, int(raw))
        except ValueError:
            return DEFAULT_MODAL_SUBMIT_STALE_SECONDS

    @contextmanager
    def _translate_runtime_errors(self):
        try:
            yield
        except BackendPermissionError as exc:
            raise PermissionDeniedError(str(exc)) from exc
        except BackendUnavailableError as exc:
            raise ValidationError(str(exc), details={"retryable": True}) from exc
        except BackendValidationError as exc:
            raise ValidationError(str(exc)) from exc


def _submit_owner_available(*, job: dict[str, Any]) -> bool:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    owner_pid = _int_or_none(metadata.get(SUBMIT_OWNER_PID_KEY))
    if owner_pid is not None and owner_pid != os.getpid():
        return _process_alive(pid=owner_pid)
    return _submit_worker_alive(job_id=str(job["id"]))


def _submit_worker_alive(*, job_id: str) -> bool:
    expected_name = _submit_worker_name(job_id=job_id)
    return any(
        thread.name == expected_name and thread.is_alive()
        for thread in threading.enumerate()
    )


def _submit_worker_name(*, job_id: str) -> str:
    return f"research-plugin-submit-{job_id}"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _process_alive(*, pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        timestamp = value.strip()
        if timestamp.endswith("Z"):
            timestamp = f"{timestamp[:-1]}+00:00"
        dt = datetime.fromisoformat(timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - dt.astimezone(UTC)).total_seconds())


def _format_exc(exc: BaseException) -> str:
    """Prefix the exception class name so bare strings like 'No item with that key'
    don't bury the actual error type in the DB."""
    return f"{type(exc).__name__}: {exc}"
