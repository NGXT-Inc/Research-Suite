"""Submission pipeline for Modal jobs."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ...errors import BackendPermissionError
from ...types import ExecutionProgress, JobSpec, ProgressCallback, SubmitStatusReport
from .config import ModalConfig, ModalJobHints, parse_modal_hints
from .runner import build_runtime_job_id, write_runner_files

if TYPE_CHECKING:
    from .backend import ModalExecutionBackend


STAGE_MESSAGES: tuple[tuple[str, str], ...] = (
    ("preparing", "Preparing execution environment"),
    ("volume", "Ensuring project volume"),
    ("syncing", "Synchronizing workspace with volume"),
    ("conflict_gate", "Checking for sync conflicts"),
    ("acquiring_sandbox", "Acquiring sandbox"),
    ("encoding", "Encoding runtime job id"),
    ("starting", "Starting execution"),
)
STAGE_NAMES = tuple(name for name, _message in STAGE_MESSAGES)
MESSAGE_BY_STAGE = dict(STAGE_MESSAGES)


@dataclass
class SubmitContext:
    """State carried across pipeline stages."""

    spec: JobSpec
    progress: ProgressCallback | None = None

    # set by _prepare
    project_id: str = ""
    hints: ModalJobHints | None = None
    job_config: ModalConfig | None = None

    # set by _ensure_volume
    volume_name: str = ""

    # set by _acquire_sandbox
    compatibility_key: tuple[Any, ...] = ()
    sandbox: Any = None
    sandbox_id: str = ""

    # set by _encode_runtime_job_id
    runtime_job_id: str = ""


class SubmissionPipeline:
    """Orchestrates submit stages and cleanup."""

    def __init__(self, *, backend: ModalExecutionBackend) -> None:
        self._backend = backend

        # status_report() reads this state while run() advances stages.
        self._state_lock = threading.Lock()
        self._current: str | None = None
        self._completed_names: list[str] = []
        self._failed_at: str | None = None
        self._runtime_job_id: str = ""

    def run(self, *, spec: JobSpec, progress: ProgressCallback | None = None) -> str:
        ctx = SubmitContext(spec=spec, progress=progress)
        try:
            self._run_stage(ctx, "preparing", self._prepare)
            self._run_stage(ctx, "volume", self._ensure_volume)
            self._run_stage(ctx, "syncing", self._sync)
            self._run_stage(ctx, "conflict_gate", self._conflict_gate)
            self._run_stage(ctx, "acquiring_sandbox", self._acquire_sandbox)
            self._run_stage(ctx, "encoding", self._encode_runtime_job_id)
            self._run_stage(ctx, "starting", self._start_runner)
            with self._state_lock:
                self._current = None
            return ctx.runtime_job_id
        except BaseException:
            # Cleanup should still run for KeyboardInterrupt/SystemExit.
            with self._state_lock:
                self._failed_at = self._current
                self._current = None
            self._terminate_sandbox(ctx)
            raise

    def _run_stage(self, ctx: SubmitContext, name: str, fn) -> None:
        with self._state_lock:
            self._current = name
        self._emit_stage_progress(name=name, ctx=ctx)
        fn(ctx)
        with self._state_lock:
            self._completed_names.append(name)
            self._runtime_job_id = ctx.runtime_job_id

    def _emit_stage_progress(self, *, name: str, ctx: SubmitContext) -> None:
        """Emit the current submit stage."""
        if ctx.progress is None:
            return
        message = MESSAGE_BY_STAGE.get(name, name)
        metadata: dict[str, str] = {}
        if ctx.sandbox_id:
            metadata["sandbox_id"] = ctx.sandbox_id
        if ctx.hints is not None:
            metadata["gpu"] = ctx.hints.gpu
        ctx.progress(
            ExecutionProgress(
                phase=name,
                message=message,
                runtime_job_id=ctx.runtime_job_id or None,
                metadata=metadata or None,
            )
        )

    def status_report(self) -> SubmitStatusReport:
        """Snapshot submit progress."""
        with self._state_lock:
            return SubmitStatusReport(
                stages=STAGE_NAMES,
                current=self._current,
                completed=tuple(self._completed_names),
                failed_at=self._failed_at,
                runtime_job_id=self._runtime_job_id,
            )

    def _prepare(self, ctx: SubmitContext) -> None:
        ctx.project_id = ctx.spec.metadata.get("project_id") or "default"
        ctx.hints = parse_modal_hints(
            backend_hints=ctx.spec.backend_hints,
            config=self._backend.config,
        )
        ctx.job_config = _config_for_job(config=self._backend.config, spec=ctx.spec)

    def _ensure_volume(self, ctx: SubmitContext) -> None:
        info = self._backend.sync_engine.ensure_project_volume(project_id=ctx.project_id)
        ctx.volume_name = info["volume_name"]

    def _sync(self, ctx: SubmitContext) -> None:
        self._backend.sync_engine.sync(project_id=ctx.project_id)

    def _conflict_gate(self, ctx: SubmitContext) -> None:
        unresolved = self._backend.baseline.conflict_paths(project_id=ctx.project_id)
        if unresolved:
            raise BackendPermissionError(
                f"cannot submit: {len(unresolved)} unresolved sync conflict(s); "
                "resolve before retrying"
            )

    def _acquire_sandbox(self, ctx: SubmitContext) -> None:
        volume = self._backend._provide_volume(ctx.volume_name)
        volumes = {self._backend.config.remote_workdir: volume}
        ctx.compatibility_key = (
            *ctx.hints.compatibility_key,
            ctx.volume_name,
            self._backend.config.remote_workdir,
        )
        ctx.sandbox = self._backend.runtime.get_or_create_sandbox(
            hints=ctx.hints,
            metadata=ctx.spec.metadata,
            volumes=volumes,
            compatibility_key=ctx.compatibility_key,
        )
        ctx.sandbox_id = str(getattr(ctx.sandbox, "object_id", ""))

    def _encode_runtime_job_id(self, ctx: SubmitContext) -> None:
        ctx.runtime_job_id = build_runtime_job_id(
            sandbox_id=ctx.sandbox_id,
            job_id=ctx.spec.metadata.get("research_plugin_job_id", ""),
            experiment_id=ctx.spec.metadata.get("experiment_id", ""),
            project_id=ctx.project_id,
            remote_workdir=ctx.job_config.remote_workdir,
            runner_dir=ctx.job_config.runner_dir,
            volume_name=ctx.volume_name,
            compatibility_key=ctx.compatibility_key,
        )
        self._backend._sandboxes[ctx.runtime_job_id] = ctx.sandbox

    def _start_runner(self, ctx: SubmitContext) -> None:
        self._backend._reload_sandbox_volumes(sandbox=ctx.sandbox)
        write_runner_files(
            sandbox=ctx.sandbox,
            spec=ctx.spec,
            config=ctx.job_config,
            hints=ctx.hints,
            volume_name=ctx.volume_name,
        )

    def _terminate_sandbox(self, ctx: SubmitContext) -> None:
        if ctx.sandbox is None:
            return
        try:
            self._backend._cleanup_orphaned_sandbox(
                sandbox=ctx.sandbox,
                runtime_job_id=ctx.runtime_job_id or None,
                reason="submit_failed_after_create",
            )
        except Exception:  # noqa: BLE001
            pass


def _config_for_job(*, config: ModalConfig, spec: JobSpec) -> ModalConfig:
    """Per-job ModalConfig with an isolated runner_dir."""
    job_id = spec.metadata.get("research_plugin_job_id") or "job"
    return replace(config, runner_dir=_runner_dir_for_job(config=config, job_id=job_id))


def _runner_dir_for_job(*, config: ModalConfig, job_id: str) -> str:
    leaf = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_id).strip("._-")[:80] or "job"
    return f"{config.runner_dir.rstrip('/')}/jobs/{leaf}"
