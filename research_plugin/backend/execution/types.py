"""Backend-neutral execution types, the ExecutionBackend protocol, and shared
output-status helpers.

Everything in this module is dependency-free with respect to the rest of the
app: backends and the JobService talk only through these contracts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence


JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]

TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class JobSpec:
    command: str
    cwd: str
    env: Mapping[str, str]
    expected_outputs: Sequence[str]
    backend_hints: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JobStatus:
    state: JobState
    runtime_job_id: str
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # Optional finer-grained substate within `state`. Used to distinguish
    # queued.waiting_sandbox (Modal hasn't provisioned the GPU yet) from
    # queued.runner_starting (sandbox ready, runner hasn't written
    # state=running). Surfaces in the API as the nested_status suffix.
    # None means "no useful substate" — most state/backend combinations.
    phase: str | None = None


@dataclass(frozen=True)
class ExecutionProgress:
    phase: str
    message: str = ""
    runtime_job_id: str | None = None
    metadata: Mapping[str, str] | None = None


ProgressCallback = Callable[[ExecutionProgress], None]


@dataclass(frozen=True)
class SubmitStatusReport:
    """Snapshot of an in-flight submission's progress, safe to consume from
    any thread. Returned by backends that expose a multi-stage submission
    pipeline (currently Modal); other backends may return None.

    Semantics:
      • not started: current is None, completed is empty, failed_at is None
      • running stage X: current == X, completed contains the prefix before X
      • finished: current is None, completed contains every stage name,
        failed_at is None
      • failed at stage X: current is None, failed_at == X, completed
        contains the successful prefix before X (X itself is NOT in completed)
    """

    stages: tuple[str, ...]
    current: str | None
    completed: tuple[str, ...]
    failed_at: str | None = None
    runtime_job_id: str = ""


@dataclass(frozen=True)
class OutputStatus:
    path: str
    exists: bool
    is_file: bool


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    supports_local_working_dir: bool
    materializes_outputs: bool


# ---------------------------------------------------------------------------
# Execution backend protocol
# ---------------------------------------------------------------------------


class ExecutionBackend(Protocol):
    capabilities: BackendCapabilities

    def submit(self, *, spec: JobSpec, progress: ProgressCallback | None = None) -> str: ...

    def status(self, *, runtime_job_id: str) -> JobStatus: ...

    def logs(self, *, runtime_job_id: str) -> str: ...

    def cancel(self, *, runtime_job_id: str) -> bool: ...

    def materialize_outputs(
        self,
        *,
        runtime_job_id: str,
        expected_outputs: Sequence[str],
        repo_root: Path,
    ) -> Sequence[OutputStatus]: ...

    def health(self) -> dict: ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def local_output_statuses(
    *, repo_root: Path, expected_outputs: Sequence[str]
) -> list[OutputStatus]:
    """Canonical no-op materialization for backends whose job outputs are
    already on the local filesystem (e.g. Ray local cluster, fake backend)."""
    results: list[OutputStatus] = []
    for rel_path in expected_outputs:
        path = repo_root / rel_path
        exists = path.exists()
        results.append(
            OutputStatus(
                path=rel_path,
                exists=exists,
                is_file=path.is_file() if exists else False,
            )
        )
    return results
