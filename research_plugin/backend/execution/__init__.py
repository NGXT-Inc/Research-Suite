"""Backend-neutral execution subsystem.

Public surface (re-exported here for stable imports from the rest of the app):
    ExecutionBackend, JobExecutionPolicy, JobSpec, JobStatus, ExecutionProgress,
    OutputStatus, BackendCapabilities, build_execution_backend,
    TERMINAL_STATUSES, and backend errors.

The runtime contract (ExecutionBackend protocol, JobSpec/JobStatus dataclasses,
output-status helper) lives in `.types`. The backend implementations live under
`.backends`. The selection factory `build_execution_backend` is defined here
because it *is* the public entry point.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .errors import (
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    ExecutionBackendError,
)
from .policy import (
    ALLOWED_EXECUTABLES,
    FORBIDDEN_SHELL_TOKENS,
    JobExecutionPolicy,
    SENSITIVE_ENV_MARKERS,
)
from .types import (
    TERMINAL_STATUSES,
    BackendCapabilities,
    ExecutionBackend,
    ExecutionProgress,
    JobSpec,
    JobStatus,
    OutputStatus,
    ProgressCallback,
    SubmitStatusReport,
)


ActivityHook = Callable[[str, dict[str, Any]], None]
ShouldPollProject = Callable[[str], bool]


def build_execution_backend(
    *,
    repo_root: Path,
    name: str | None = None,
    activity: ActivityHook | None = None,
    should_poll_project: ShouldPollProject | None = None,
) -> ExecutionBackend:
    """Select and construct the configured execution backend.

    Backend name comes from (in order): `name=` arg,
    `RESEARCH_PLUGIN_EXECUTION_BACKEND` env, or "modal" by default.
    """
    selected = (name or os.environ.get("RESEARCH_PLUGIN_EXECUTION_BACKEND") or "modal").strip().lower()
    if selected == "fake":
        from .backends.fake import FakeBackend

        return FakeBackend()
    if selected == "ray":
        from .backends.ray import build_ray_backend

        return build_ray_backend(repo_root=repo_root)
    if selected == "modal":
        from .backends.modal import build_modal_backend

        return build_modal_backend(
            repo_root=repo_root,
            activity=activity,
            should_poll_project=should_poll_project,
        )
    raise BackendUnavailableError(f"unknown execution backend: {selected}")


__all__ = [
    "ALLOWED_EXECUTABLES",
    "ActivityHook",
    "BackendCapabilities",
    "BackendPermissionError",
    "BackendUnavailableError",
    "BackendValidationError",
    "ExecutionBackend",
    "ExecutionBackendError",
    "ExecutionProgress",
    "FORBIDDEN_SHELL_TOKENS",
    "JobExecutionPolicy",
    "JobSpec",
    "JobStatus",
    "OutputStatus",
    "ProgressCallback",
    "SENSITIVE_ENV_MARKERS",
    "ShouldPollProject",
    "SubmitStatusReport",
    "TERMINAL_STATUSES",
    "build_execution_backend",
]
