"""ExecutionBackend implementation for Ray Jobs."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Sequence

from ...types import local_output_statuses
from ...errors import BackendValidationError
from ...types import (
    BackendCapabilities,
    ExecutionProgress,
    JobSpec,
    JobStatus,
    OutputStatus,
    ProgressCallback,
)
from .clients import RayClient, RayRestJobClient, RaySdkJobClient


RAY_TO_LOCAL_STATUS: dict[str, str] = {
    "PENDING": "queued",
    "RUNNING": "running",
    "STOPPED": "cancelled",
    "SUCCEEDED": "succeeded",
    "FAILED": "failed",
}


class RayExecutionBackend:
    def __init__(self, *, repo_root: Path, client: RayClient) -> None:
        self.repo_root = repo_root.resolve()
        self.client = client
        self.capabilities = BackendCapabilities(
            name="ray",
            supports_local_working_dir=bool(client.supports_local_working_dir_upload),
            materializes_outputs=False,
        )

    def submit(self, *, spec: JobSpec, progress: ProgressCallback | None = None) -> str:
        if progress is not None:
            progress(ExecutionProgress(phase="starting", message="Submitting to execution backend"))
        runtime_env = self._runtime_env(spec=spec)
        entrypoint = self._entrypoint(spec=spec, runtime_env=runtime_env)
        return self.client.submit_job(
            entrypoint=entrypoint,
            runtime_env=runtime_env,
            metadata=dict(spec.metadata),
        )

    def status(self, *, runtime_job_id: str) -> JobStatus:
        native = self.client.get_job_status(runtime_job_id=runtime_job_id)
        return JobStatus(
            state=self._map_status(native),
            runtime_job_id=runtime_job_id,
        )

    def logs(self, *, runtime_job_id: str) -> str:
        return self.client.get_job_logs(runtime_job_id=runtime_job_id)

    def cancel(self, *, runtime_job_id: str) -> bool:
        return self.client.stop_job(runtime_job_id=runtime_job_id)

    def materialize_outputs(
        self,
        *,
        runtime_job_id: str,
        expected_outputs: Sequence[str],
        repo_root: Path,
    ) -> Sequence[OutputStatus]:
        # Ray runs on the local cluster; outputs already live in the repo.
        del runtime_job_id
        return local_output_statuses(repo_root=repo_root, expected_outputs=expected_outputs)

    def health(self) -> dict:
        return self.client.health()

    def _runtime_env(self, *, spec: JobSpec) -> dict[str, Any]:
        runtime_env = dict(spec.backend_hints)
        working_dir = runtime_env.get("working_dir")
        if (
            working_dir
            and not self.capabilities.supports_local_working_dir
            and "://" not in str(working_dir)
        ):
            raise BackendValidationError(
                "REST Ray mode requires backend_hints.working_dir to be a remote URI, or omitted"
            )
        if self._uses_runtime_working_dir(runtime_env=runtime_env):
            runtime_env.setdefault("working_dir", str(self.repo_root))
        if spec.env:
            env_vars = dict(runtime_env.get("env_vars", {}))
            env_vars.update(spec.env)
            runtime_env["env_vars"] = env_vars
        return runtime_env

    def _entrypoint(self, *, spec: JobSpec, runtime_env: dict[str, Any]) -> str:
        if self._uses_runtime_working_dir(runtime_env=runtime_env):
            if spec.cwd == ".":
                return spec.command
            return f"cd {shlex.quote(spec.cwd)} && {spec.command}"
        absolute_cwd = (self.repo_root / spec.cwd).resolve()
        return f"cd {shlex.quote(str(absolute_cwd))} && {spec.command}"

    def _uses_runtime_working_dir(self, *, runtime_env: dict[str, Any]) -> bool:
        return "working_dir" in runtime_env or self.capabilities.supports_local_working_dir

    def _map_status(self, native: str) -> str:
        return RAY_TO_LOCAL_STATUS.get(native.upper(), native.lower())


def build_ray_backend(*, repo_root: Path) -> RayExecutionBackend:
    address = os.environ.get("RESEARCH_PLUGIN_RAY_ADDRESS", "http://127.0.0.1:8265")
    try:
        client: RayClient = RaySdkJobClient(address=address)
    except Exception:
        client = RayRestJobClient(address=address)
    return RayExecutionBackend(repo_root=repo_root, client=client)
