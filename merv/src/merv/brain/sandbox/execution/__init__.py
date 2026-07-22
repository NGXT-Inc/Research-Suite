"""Sandbox backend factory and compatibility exports.

The backend port lives in ``merv.brain.sandbox.sandbox_backend``. Backend implementations
and their factory live under this execution package.
"""

from __future__ import annotations

from pathlib import Path

from ...kernel.env import env_value
from ..sandbox_backend import (
    SANDBOX_STATES,
    BackendCapabilities,
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    CapacityUnavailableError,
    ExecutionBackendError,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxBackend,
    SandboxBackendBase,
    SandboxDriver,
    SandboxRequest,
)
from .driver_registry import (
    DEFAULT_SANDBOX_DRIVER,
    SANDBOX_DRIVER_ALIASES,
    SANDBOX_DRIVER_DESCRIPTORS,
    ActivityHook,
    SandboxDriverDescriptor,
    SandboxDriverFactory,
    build_sandbox_driver,
    canonical_sandbox_driver_name,
    sandbox_driver_descriptor,
    sandbox_driver_inventory,
)


BACKEND_ALIASES = SANDBOX_DRIVER_ALIASES


def _canonical_backend_name(name: str) -> str:
    return canonical_sandbox_driver_name(name)


def _build_named_backend(
    *,
    name: str,
    repo_root: Path,
    activity: ActivityHook | None = None,
) -> SandboxBackend:
    return build_sandbox_driver(
        name=name,
        repo_root=repo_root,
        activity=activity,
    )


def build_sandbox_backend(
    *,
    repo_root: Path,
    name: str | None = None,
    activity: ActivityHook | None = None,
) -> SandboxBackend:
    """Select and construct the configured sandbox backend(s).

    Backend name comes from (in order): `name=` arg,
    `MERV_EXECUTION_BACKEND` env (legacy `RESEARCH_PLUGIN_EXECUTION_BACKEND`),
    or "lambda_labs" by default. `MERV_EXECUTION_BACKENDS` (comma-separated,
    legacy `RESEARCH_PLUGIN_EXECUTION_BACKENDS`) configures several providers
    at once behind one MultiplexingSandboxBackend; the single-name env then
    selects the default among them. One configured backend keeps today's
    direct, prefix-free path.
    """
    if name is not None:
        return _build_named_backend(
            name=_canonical_backend_name(name), repo_root=repo_root, activity=activity
        )
    configured = list(
        dict.fromkeys(  # de-dupe, keep configured order
            _canonical_backend_name(part)
            for part in (env_value("MERV_EXECUTION_BACKENDS") or "").split(",")
            if part.strip()
        )
    )
    single = _canonical_backend_name(env_value("MERV_EXECUTION_BACKEND") or "")
    if len(configured) <= 1:
        return _build_named_backend(
            name=configured[0] if configured else (single or DEFAULT_SANDBOX_DRIVER),
            repo_root=repo_root,
            activity=activity,
        )
    from .multiplexer import MultiplexingSandboxBackend

    backends = {
        backend_name: _build_named_backend(
            name=backend_name, repo_root=repo_root, activity=activity
        )
        for backend_name in configured
    }
    default = single if single in backends else configured[0]
    return MultiplexingSandboxBackend(
        backends=backends, default=default, aliases=BACKEND_ALIASES
    )


__all__ = [
    "ActivityHook",
    "BACKEND_ALIASES",
    "DEFAULT_SANDBOX_DRIVER",
    "BackendCapabilities",
    "BackendPermissionError",
    "BackendUnavailableError",
    "BackendValidationError",
    "CapacityUnavailableError",
    "ExecutionBackendError",
    "OnCreated",
    "OnPhase",
    "ProvisionedSandbox",
    "SANDBOX_DRIVER_ALIASES",
    "SANDBOX_DRIVER_DESCRIPTORS",
    "SANDBOX_STATES",
    "SandboxBackend",
    "SandboxBackendBase",
    "SandboxDriver",
    "SandboxDriverDescriptor",
    "SandboxDriverFactory",
    "SandboxRequest",
    "build_sandbox_driver",
    "build_sandbox_backend",
    "canonical_sandbox_driver_name",
    "sandbox_driver_descriptor",
    "sandbox_driver_inventory",
]
