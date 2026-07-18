"""Sandbox backend factory and compatibility exports.

The backend port lives in ``backend.sandbox.sandbox_backend``. Backend implementations
and their factory live under this execution package.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ..env import env_value
from ..sandbox.sandbox_backend import (
    SANDBOX_STATES,
    BackendCapabilities,
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    ExecutionBackendError,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxBackend,
    SandboxBackendBase,
    SandboxRequest,
)


ActivityHook = Callable[[str, dict[str, Any]], None]


def build_sandbox_backend(
    *,
    repo_root: Path,
    name: str | None = None,
    activity: ActivityHook | None = None,
) -> SandboxBackend:
    """Select and construct the configured sandbox backend.

    Backend name comes from (in order): `name=` arg,
    `MERV_EXECUTION_BACKEND` env (legacy `RESEARCH_PLUGIN_EXECUTION_BACKEND`),
    or "lambda_labs" by default.
    """
    selected = (
        name or env_value("MERV_EXECUTION_BACKEND") or "lambda_labs"
    ).strip().lower()
    if selected == "fake":
        from .backends.fake import FakeSandboxBackend

        return FakeSandboxBackend()
    if selected == "modal":
        from .backends.modal import build_modal_sandbox_backend

        return build_modal_sandbox_backend(
            repo_root=repo_root,
            activity=activity,
        )
    if selected in {"thunder", "thunder_compute", "thundercompute"}:
        from .backends.thunder_compute import build_thunder_compute_sandbox_backend

        return build_thunder_compute_sandbox_backend(repo_root=repo_root)
    if selected in {"lambda", "lambda_labs", "lambdalabs"}:
        from .backends.lambda_labs import build_lambda_labs_sandbox_backend

        return build_lambda_labs_sandbox_backend(repo_root=repo_root)
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
    "SandboxBackendBase",
    "SandboxRequest",
    "build_sandbox_backend",
]
