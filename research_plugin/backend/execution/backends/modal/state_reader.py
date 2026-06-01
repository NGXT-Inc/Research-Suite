"""Status and log reads for Modal jobs."""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from typing import Any, Callable

from ...types import JobStatus, TERMINAL_STATUSES
from .config import ModalConfig
from .runner import (
    RUNNER_STATUS,
    RUNNER_STDERR,
    RUNNER_STDOUT,
    STDERR_MAX,
    STDOUT_MAX,
    RuntimeJobRef,
    decode_runtime_job_id,
    format_logs,
    read_logs,
    read_status,
    runner_file_volume_path,
    status_from_text,
)


SandboxProvider = Callable[..., Any]
VolumeProvider = Callable[[str], Any]
RetainSandbox = Callable[..., None]


class ModalStateReader:
    def __init__(
        self,
        *,
        config: ModalConfig,
        sandbox_provider: SandboxProvider,
        volume_provider: VolumeProvider,
        retain_sandbox: RetainSandbox,
    ) -> None:
        self.config = config
        self.sandbox_provider = sandbox_provider
        self.volume_provider = volume_provider
        self.retain_sandbox = retain_sandbox

    def status(self, *, runtime_job_id: str, timeout_seconds: float) -> JobStatus:
        ref = decode_runtime_job_id(runtime_job_id)
        try:
            sandbox = self.sandbox_provider(runtime_job_id=runtime_job_id, ref=ref)
        except Exception as exc:  # noqa: BLE001
            volume_status = self._read_status_from_volume(
                ref=ref,
                runtime_job_id=runtime_job_id,
            )
            if volume_status is not None:
                return _with_runner_starting_phase(volume_status)
            return JobStatus(state="failed", runtime_job_id=runtime_job_id, error=str(exc))

        ready_error = _sandbox_ready_error(sandbox=sandbox)
        if ready_error:
            return self._status_for_not_ready_sandbox(
                runtime_job_id=runtime_job_id,
                ref=ref,
                sandbox=sandbox,
                ready_error=ready_error,
                timeout_seconds=timeout_seconds,
            )

        try:
            status = _call_with_timeout(
                func=lambda: read_status(
                    sandbox=sandbox,
                    config=ref.config_for(self.config),
                    runtime_job_id=runtime_job_id,
                ),
                timeout_seconds=timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            volume_status = self._read_status_from_volume(
                ref=ref,
                runtime_job_id=runtime_job_id,
            )
            if volume_status is not None:
                return self._postprocess_status(status=volume_status, ref=ref, sandbox=sandbox)
            return JobStatus(state="running", runtime_job_id=runtime_job_id, error=None)

        if status is None:
            volume_status = self._read_status_from_volume(
                ref=ref,
                runtime_job_id=runtime_job_id,
            )
            if volume_status is not None:
                return self._postprocess_status(status=volume_status, ref=ref, sandbox=sandbox)
            return JobStatus(state="running", runtime_job_id=runtime_job_id, error=None)

        if status.error and status.error.startswith("Modal status is not available yet:"):
            volume_status = self._read_status_from_volume(
                ref=ref,
                runtime_job_id=runtime_job_id,
            )
            if volume_status is not None:
                return self._postprocess_status(status=volume_status, ref=ref, sandbox=sandbox)

        return self._postprocess_status(status=status, ref=ref, sandbox=sandbox)

    def logs(self, *, runtime_job_id: str, timeout_seconds: float) -> str:
        ref = decode_runtime_job_id(runtime_job_id)
        try:
            sandbox = self.sandbox_provider(runtime_job_id=runtime_job_id, ref=ref)
        except Exception as exc:  # noqa: BLE001
            volume_logs = self._read_logs_from_volume(ref=ref)
            return volume_logs or f"Modal logs are not available: {exc}"

        try:
            logs = _call_with_timeout(
                func=lambda: read_logs(sandbox=sandbox, config=ref.config_for(self.config)),
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            volume_logs = self._read_logs_from_volume(ref=ref)
            return volume_logs or f"Modal logs are not available: {exc}"

        if logs is None:
            volume_logs = self._read_logs_from_volume(ref=ref)
            return volume_logs or (
                "Modal logs are not available: sandbox log read timed out and no "
                "committed Volume logs were readable"
            )
        if not logs:
            return self._read_logs_from_volume(ref=ref) or logs
        return logs

    def _status_for_not_ready_sandbox(
        self,
        *,
        runtime_job_id: str,
        ref: RuntimeJobRef,
        sandbox: Any,
        ready_error: str,
        timeout_seconds: float,
    ) -> JobStatus:
        volume_status = self._read_status_from_volume(
            ref=ref,
            runtime_job_id=runtime_job_id,
        )
        if volume_status is not None and volume_status.state in TERMINAL_STATUSES:
            return self._postprocess_status(status=volume_status, ref=ref, sandbox=sandbox)
        if _sandbox_finished(sandbox=sandbox, timeout_seconds=timeout_seconds):
            return JobStatus(
                state="failed",
                runtime_job_id=runtime_job_id,
                error=(
                    "Modal sandbox terminated before the job finished and left "
                    f"no committed status on the Volume ({ready_error})"
                ),
            )
        return JobStatus(
            state="queued",
            runtime_job_id=runtime_job_id,
            error=ready_error,
            phase="waiting_sandbox",
        )

    def _postprocess_status(
        self,
        *,
        status: JobStatus,
        ref: RuntimeJobRef,
        sandbox: Any,
    ) -> JobStatus:
        status = _with_runner_starting_phase(status)
        if status.state in {"failed", "cancelled"}:
            self.retain_sandbox(ref=ref, sandbox=sandbox)
        return status

    def _read_status_from_volume(
        self,
        *,
        ref: RuntimeJobRef,
        runtime_job_id: str,
    ) -> JobStatus | None:
        rel_path = runner_file_volume_path(ref=ref, filename=RUNNER_STATUS)
        if rel_path is None:
            return None
        try:
            raw = self._read_volume_text(ref=ref, rel_path=rel_path)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            return JobStatus(
                state="running",
                runtime_job_id=runtime_job_id,
                error=f"Modal Volume status is not available yet: {exc}",
            )
        return status_from_text(raw=raw, runtime_job_id=runtime_job_id, source="Modal Volume")

    def _read_logs_from_volume(self, *, ref: RuntimeJobRef) -> str:
        stdout = self._read_runner_volume_text(
            ref=ref,
            filename=RUNNER_STDOUT,
            limit=STDOUT_MAX,
        )
        stderr = self._read_runner_volume_text(
            ref=ref,
            filename=RUNNER_STDERR,
            limit=STDERR_MAX,
        )
        return format_logs(stdout=stdout, stderr=stderr)

    def _read_runner_volume_text(
        self,
        *,
        ref: RuntimeJobRef,
        filename: str,
        limit: int,
    ) -> str:
        rel_path = runner_file_volume_path(ref=ref, filename=filename)
        if rel_path is None:
            return ""
        try:
            return self._read_volume_text(ref=ref, rel_path=rel_path, limit=limit)
        except FileNotFoundError:
            return ""
        except Exception:
            return ""

    def _read_volume_text(
        self,
        *,
        ref: RuntimeJobRef,
        rel_path: str,
        limit: int | None = None,
    ) -> str:
        if not ref.volume_name:
            raise FileNotFoundError(rel_path)
        volume = self.volume_provider(ref.volume_name)
        chunks: list[bytes] = []
        for chunk in volume.read_file(rel_path):
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            chunks.append(bytes(chunk))
        data = b"".join(chunks)
        if limit is not None and len(data) > limit:
            data = data[-limit:]
        return data.decode("utf-8", errors="replace")


def _sandbox_ready_error(*, sandbox: Any) -> str | None:
    wait_until_ready = getattr(sandbox, "wait_until_ready", None)
    if not callable(wait_until_ready):
        return None
    try:
        wait_until_ready(timeout=1)
        return None
    except Exception as exc:  # noqa: BLE001
        if "readiness probe" in str(exc):
            return None
        return f"Modal sandbox is not ready yet: {exc}"


def _sandbox_finished(*, sandbox: Any, timeout_seconds: float) -> bool:
    poll = getattr(sandbox, "poll", None)
    if not callable(poll):
        return False
    try:
        result = _call_with_timeout(func=poll, timeout_seconds=timeout_seconds)
    except Exception:  # noqa: BLE001
        return False
    return result is not None


def _with_runner_starting_phase(status: JobStatus) -> JobStatus:
    if status.state == "queued" and status.phase is None:
        return replace(status, phase="runner_starting")
    return status


def _call_with_timeout(*, func: Callable[[], Any], timeout_seconds: float) -> Any | None:
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result_queue.put((True, func()))
        except Exception as exc:  # noqa: BLE001
            result_queue.put((False, exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        ok, value = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        return None
    if ok:
        return value
    raise value
