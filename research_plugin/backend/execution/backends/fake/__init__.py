"""In-memory execution backend for JobService tests."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ...types import local_output_statuses
from ...types import (
    BackendCapabilities,
    ExecutionProgress,
    JobSpec,
    JobStatus,
    OutputStatus,
    ProgressCallback,
)


class FakeBackend:
    def __init__(self, *, materializes: bool = False) -> None:
        self.capabilities = BackendCapabilities(
            name="fake",
            supports_local_working_dir=True,
            materializes_outputs=materializes,
        )
        self.counter = 0
        self.submitted: dict[str, JobSpec] = {}
        self.statuses: dict[str, str] = {}
        self.errors: dict[str, str | None] = {}
        self.logs_by_id: dict[str, str] = {}
        self.status_calls: list[str] = []
        self.materialize_calls: list[str] = []
        self.cancelled: list[str] = []

    def submit(self, *, spec: JobSpec, progress: ProgressCallback | None = None) -> str:
        if progress is not None:
            progress(ExecutionProgress(phase="starting", message="Starting execution"))
        self.counter += 1
        runtime_job_id = f"fake_job_{self.counter}"
        self.submitted[runtime_job_id] = spec
        self.statuses[runtime_job_id] = "queued"
        self.errors[runtime_job_id] = None
        self.logs_by_id[runtime_job_id] = "submitted\n"
        return runtime_job_id

    def status(self, *, runtime_job_id: str) -> JobStatus:
        self.status_calls.append(runtime_job_id)
        return JobStatus(
            state=self.statuses[runtime_job_id],  # type: ignore[arg-type]
            runtime_job_id=runtime_job_id,
            error=self.errors.get(runtime_job_id),
        )

    def logs(self, *, runtime_job_id: str) -> str:
        return self.logs_by_id.get(runtime_job_id, "")

    def cancel(self, *, runtime_job_id: str) -> bool:
        self.statuses[runtime_job_id] = "cancelled"
        self.cancelled.append(runtime_job_id)
        return True

    def materialize_outputs(
        self,
        *,
        runtime_job_id: str,
        expected_outputs: Sequence[str],
        repo_root: Path,
    ) -> Sequence[OutputStatus]:
        self.materialize_calls.append(runtime_job_id)
        return local_output_statuses(repo_root=repo_root, expected_outputs=expected_outputs)

    def health(self) -> dict:
        return {"ok": True, "name": self.capabilities.name}

    def set_status(self, *, runtime_job_id: str, state: str, error: str | None = None) -> None:
        self.statuses[runtime_job_id] = state
        self.errors[runtime_job_id] = error

    @property
    def last_runtime_job_id(self) -> str:
        return f"fake_job_{self.counter}"
