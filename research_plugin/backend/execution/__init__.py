"""Backend-neutral sandbox-execution subsystem.

The runtime contract (SandboxBackend protocol, SandboxRequest/ProvisionedSandbox
dataclasses) lives in `.types`. Backend implementations live under `.backends`.
The selection factory `build_sandbox_backend` is the public entry point.
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
from .types import (
    SANDBOX_STATES,
    BackendCapabilities,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxBackend,
    SandboxRequest,
)


ActivityHook = Callable[[str, dict[str, Any]], None]
ShouldPollProject = Callable[[str], bool]


def build_sandbox_backend(
    *,
    repo_root: Path,
    name: str | None = None,
    activity: ActivityHook | None = None,
    should_poll_project: ShouldPollProject | None = None,
) -> SandboxBackend:
    """Select and construct the configured sandbox backend.

    Backend name comes from (in order): `name=` arg,
    `RESEARCH_PLUGIN_EXECUTION_BACKEND` env, or "modal" by default.
    """
    selected = (
        name or os.environ.get("RESEARCH_PLUGIN_EXECUTION_BACKEND") or "modal"
    ).strip().lower()
    if selected == "fake":
        from .backends.fake import FakeSandboxBackend

        return FakeSandboxBackend()
    if selected == "modal":
        from .backends.modal import build_modal_sandbox_backend

        return build_modal_sandbox_backend(
            repo_root=repo_root,
            activity=activity,
            should_poll_project=should_poll_project,
        )
    raise BackendUnavailableError(f"unknown execution backend: {selected}")


__all__ = [
    "ActivityHook",
    "BackendCapabilities",
    "BackendPermissionError",
    "BackendUnavailableError",
    "BackendValidationError",
    "ExecutionBackendError",
    "OnCreated",
    "OnPhase",
    "ProvisionedSandbox",
    "SANDBOX_STATES",
    "SandboxBackend",
    "SandboxRequest",
    "ShouldPollProject",
    "build_sandbox_backend",
]
