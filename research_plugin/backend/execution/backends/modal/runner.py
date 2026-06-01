"""Remote runner protocol for Modal execution jobs."""

from __future__ import annotations

import base64
import json
import shlex
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ...errors import BackendValidationError
from ...types import JobSpec, JobStatus
from .config import ModalConfig, ModalJobHints
from ._sandbox_ops import (
    TRANSIENT_VOLUME_ERRORS,
    exec_checked,
    maybe_await,
    read_stream,
    wait_process,
)


STDOUT_MAX = 50_000
STDERR_MAX = 10_000
RUNNER_STATUS = "status.json"
RUNNER_STDOUT = "stdout.log"
RUNNER_STDERR = "stderr.log"
RUNNER_SCRIPT = "runner.py"
RUNNER_COMMAND = "command.sh"
RUNNER_PID = "pid"
RUNNER_SUPERVISOR_PID = "supervisor_pid"
RUNNER_CANCEL = "cancel.requested"
VALID_STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class RuntimeJobRef:
    sandbox_id: str
    job_id: str = ""
    experiment_id: str = ""
    project_id: str = "default"
    remote_workdir: str = ""
    runner_dir: str = ""
    volume_name: str = ""
    compatibility_key: tuple[Any, ...] = ()
    backend: str = "modal"
    version: int = 2

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RuntimeJobRef":
        raw_key = payload.get("compatibility_key") or ()
        if not isinstance(raw_key, (list, tuple)):
            raw_key = ()
        return cls(
            backend=str(payload.get("backend") or "modal"),
            version=_int_or_default(payload.get("version"), default=1),
            sandbox_id=str(payload.get("sandbox_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            experiment_id=str(payload.get("experiment_id") or ""),
            project_id=str(payload.get("project_id") or "default"),
            remote_workdir=str(payload.get("remote_workdir") or ""),
            runner_dir=str(payload.get("runner_dir") or ""),
            volume_name=str(payload.get("volume_name") or ""),
            compatibility_key=tuple(_jsonable_to_tuple(value) for value in raw_key),
        )

    @classmethod
    def decode(cls, runtime_job_id: str) -> "RuntimeJobRef":
        if not runtime_job_id.startswith("modal:"):
            raise BackendValidationError("invalid Modal runtime_job_id")
        raw = runtime_job_id.removeprefix("modal:")
        try:
            payload = json.loads(
                base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode("ascii"))
            )
        except Exception as exc:
            raise BackendValidationError("invalid Modal runtime_job_id") from exc
        if not isinstance(payload, dict):
            raise BackendValidationError("invalid Modal runtime_job_id")
        return cls.from_payload(payload)

    def to_payload(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "version": self.version,
            "sandbox_id": self.sandbox_id,
            "job_id": self.job_id,
            "experiment_id": self.experiment_id,
            "project_id": self.project_id,
            "remote_workdir": self.remote_workdir,
            "runner_dir": self.runner_dir,
            "volume_name": self.volume_name,
            "compatibility_key": list(self.compatibility_key),
        }

    def encode(self) -> str:
        return encode_runtime_job_id(self)

    def config_for(self, config: ModalConfig) -> ModalConfig:
        return replace(
            config,
            remote_workdir=self.remote_workdir or config.remote_workdir,
            runner_dir=self.runner_dir or config.runner_dir,
        )

    def runner_files(self, config: ModalConfig | None = None) -> "RunnerFiles":
        if config is None:
            remote_workdir = self.remote_workdir
            runner_dir = self.runner_dir
        else:
            job_config = self.config_for(config)
            remote_workdir = job_config.remote_workdir
            runner_dir = job_config.runner_dir
        return RunnerFiles(remote_workdir=remote_workdir, runner_dir=runner_dir)

    def runner_volume_path(self, filename: str) -> str | None:
        return self.runner_files().volume_path(filename)


@dataclass(frozen=True)
class RunnerFiles:
    remote_workdir: str
    runner_dir: str

    @classmethod
    def from_config(cls, config: ModalConfig) -> "RunnerFiles":
        return cls(remote_workdir=config.remote_workdir, runner_dir=config.runner_dir)

    def path(self, filename: str) -> str:
        return PurePosixPath(self.runner_dir, filename).as_posix()

    def volume_path(self, filename: str) -> str | None:
        runner_dir = PurePosixPath(self.runner_dir)
        remote_workdir = PurePosixPath(self.remote_workdir)
        if not runner_dir.is_absolute() or not remote_workdir.is_absolute():
            return None
        try:
            rel_dir = runner_dir.relative_to(remote_workdir)
        except ValueError:
            return None
        return PurePosixPath(rel_dir, filename).as_posix()

    def remote_cwd(self, spec: JobSpec) -> str:
        return PurePosixPath(self.remote_workdir, spec.cwd).as_posix()

    @property
    def runner_script(self) -> str:
        return self.path(RUNNER_SCRIPT)

    @property
    def command_script(self) -> str:
        return self.path(RUNNER_COMMAND)

    @property
    def status(self) -> str:
        return self.path(RUNNER_STATUS)

    @property
    def stdout(self) -> str:
        return self.path(RUNNER_STDOUT)

    @property
    def stderr(self) -> str:
        return self.path(RUNNER_STDERR)

    @property
    def pid(self) -> str:
        return self.path(RUNNER_PID)

    @property
    def supervisor_pid(self) -> str:
        return self.path(RUNNER_SUPERVISOR_PID)

    @property
    def cancel(self) -> str:
        return self.path(RUNNER_CANCEL)


def build_runtime_job_id(
    *,
    sandbox_id: str,
    job_id: str,
    experiment_id: str,
    project_id: str,
    remote_workdir: str,
    runner_dir: str,
    volume_name: str,
    compatibility_key: tuple[Any, ...],
) -> str:
    return RuntimeJobRef(
        sandbox_id=sandbox_id,
        job_id=job_id,
        experiment_id=experiment_id,
        project_id=project_id,
        remote_workdir=remote_workdir,
        runner_dir=runner_dir,
        volume_name=volume_name,
        compatibility_key=compatibility_key,
    ).encode()


def encode_runtime_job_id(payload: dict[str, Any] | RuntimeJobRef) -> str:
    if isinstance(payload, RuntimeJobRef):
        payload = payload.to_payload()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "modal:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_runtime_job_id(runtime_job_id: str) -> RuntimeJobRef:
    return RuntimeJobRef.decode(runtime_job_id)


def runner_file_volume_path(*, ref: RuntimeJobRef, filename: str) -> str | None:
    return ref.runner_volume_path(filename)


def _jsonable_to_tuple(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_jsonable_to_tuple(item) for item in value)
    return value


def _int_or_default(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def write_runner_files(
    *,
    sandbox: Any,
    spec: JobSpec,
    config: ModalConfig,
    hints: ModalJobHints,
    volume_name: str | None = None,
) -> None:
    files = RunnerFiles.from_config(config)
    remote_cwd = files.remote_cwd(spec)

    status_json = json.dumps(
        {
            "state": "queued",
            "runtime_job_id": "",
            "error": None,
            "started_at": None,
            "finished_at": None,
        },
        sort_keys=True,
    )

    # Mounted Volumes require in-container writes; control-plane writes fail there.
    setup = "; ".join(
        [
            "set -eu",
            f"mkdir -p {shlex.quote(config.runner_dir)} {shlex.quote(remote_cwd)}",
            _write_file_cmd(_command_script(spec=spec, remote_cwd=remote_cwd), files.command_script),
            _write_file_cmd(_runner_script(), files.runner_script),
            _write_file_cmd(status_json, files.status),
            f": > {shlex.quote(files.stdout)}",
            f": > {shlex.quote(files.stderr)}",
        ]
    )
    # Prior sandboxes can briefly hold the Volume write lease.
    exec_checked(
        sandbox=sandbox,
        command=setup,
        timeout=120,
        retries=6,
        retry_on=TRANSIENT_VOLUME_ERRORS,
    )

    # Launch the detached supervisor (runner.py) inside the sandbox.
    if volume_name:
        volume_arg = (
            f"--volume-mount {shlex.quote(config.remote_workdir)} "
            f"--volume-name {shlex.quote(volume_name)} "
        )
    else:
        volume_arg = ""
    command = (
        f"chmod +x {shlex.quote(files.command_script)} && "
        f"nohup python3 {shlex.quote(files.runner_script)} "
        f"--status {shlex.quote(files.status)} "
        f"--stdout {shlex.quote(files.stdout)} "
        f"--stderr {shlex.quote(files.stderr)} "
        f"--pid {shlex.quote(files.pid)} "
        f"--cancel {shlex.quote(files.cancel)} "
        f"--timeout {int(hints.timeout)} "
        f"--command {shlex.quote(files.command_script)} "
        f"{volume_arg}"
        f">/dev/null 2>&1 & echo $! > {shlex.quote(files.supervisor_pid)}"
    )
    process = sandbox.exec("bash", "-c", command, timeout=30)
    exit_code = wait_process(process)
    if exit_code != 0:
        stderr = read_stream(getattr(process, "stderr", None))
        raise RuntimeError(f"failed to start Modal runner: {stderr}")


def _write_file_cmd(content: str, remote_path: str) -> str:
    """Shell snippet that writes base64-encoded content in-container."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_path)}"


def read_status(*, sandbox: Any, config: ModalConfig, runtime_job_id: str) -> JobStatus:
    files = RunnerFiles.from_config(config)
    try:
        raw = maybe_await(sandbox.filesystem.read_text(files.status))
    except Exception as exc:  # noqa: BLE001
        return JobStatus(
            state="running",
            runtime_job_id=runtime_job_id,
            error=f"Modal status is not available yet: {exc}",
        )
    return status_from_text(raw=raw, runtime_job_id=runtime_job_id, source="Modal")


def status_from_text(*, raw: str, runtime_job_id: str, source: str) -> JobStatus:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return JobStatus(
            state="running",
            runtime_job_id=runtime_job_id,
            error=f"{source} status is malformed: {exc}",
        )
    state = str(payload.get("state") or "running")
    if state not in VALID_STATES:
        state = "failed"
    return JobStatus(
        state=state,  # type: ignore[arg-type]
        runtime_job_id=runtime_job_id,
        error=payload.get("error"),
        started_at=payload.get("started_at"),
        finished_at=payload.get("finished_at"),
    )


def read_logs(*, sandbox: Any, config: ModalConfig) -> str:
    files = RunnerFiles.from_config(config)
    stdout = _tail_remote_text(sandbox=sandbox, path=files.stdout, limit=STDOUT_MAX)
    stderr = _tail_remote_text(sandbox=sandbox, path=files.stderr, limit=STDERR_MAX)
    return format_logs(stdout=stdout, stderr=stderr)


def format_logs(*, stdout: str, stderr: str) -> str:
    return f"{stdout}\n[stderr]\n{stderr}" if stdout and stderr else stdout or stderr


def cancel_runner(*, sandbox: Any, config: ModalConfig) -> bool:
    files = RunnerFiles.from_config(config)
    cancelled_json = json.dumps(
        {
            "state": "cancelled",
            "error": None,
            "started_at": None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
    )
    kill_block = (
        f"if [ -f {shlex.quote(files.pid)} ]; then "
        f"pid=$(cat {shlex.quote(files.pid)}); "
        "kill -TERM -- -$pid 2>/dev/null || kill -TERM $pid 2>/dev/null || true; "
        "sleep 1; "
        "kill -KILL -- -$pid 2>/dev/null || kill -KILL $pid 2>/dev/null || true; "
        f"elif [ -f {shlex.quote(files.supervisor_pid)} ]; then "
        f"kill -TERM $(cat {shlex.quote(files.supervisor_pid)}) 2>/dev/null || true; "
        "fi"
    )
    # Cancel state is written in-container because runner_dir is on a Volume.
    command = "; ".join(
        [
            f"echo 1 > {shlex.quote(files.cancel)}",
            kill_block,
            _write_file_cmd(cancelled_json, files.status),
        ]
    )
    process = sandbox.exec("bash", "-c", command, timeout=60)
    wait_process(process)
    return True


def _command_script(*, spec: JobSpec, remote_cwd: str) -> str:
    env_lines = [
        f"export {key}={shlex.quote(value)}"
        for key, value in sorted(spec.env.items())
    ]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"cd {shlex.quote(remote_cwd)}",
            *env_lines,
            f"exec {spec.command}",
            "",
        ]
    )


def _runner_script() -> str:
    return Path(__file__).with_name("_remote_runner.py").read_text(encoding="utf-8")


def _tail_remote_text(*, sandbox: Any, path: str, limit: int) -> str:
    command = f"if [ -f {shlex.quote(path)} ]; then tail -c {int(limit)} {shlex.quote(path)}; fi"
    try:
        process = sandbox.exec("bash", "-c", command, timeout=30)
        exit_code = wait_process(process)
        if exit_code != 0:
            return ""
        return read_stream(getattr(process, "stdout", None))
    except Exception:  # noqa: BLE001
        return ""
