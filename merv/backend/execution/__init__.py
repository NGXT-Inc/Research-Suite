"""Sandbox backend factory and compatibility exports.

The backend port lives in ``backend.sandbox.sandbox_backend``. Backend implementations
and their factory live under this execution package.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ..sandbox.sandbox_backend import (
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
    SandboxRequest,
)


ActivityHook = Callable[[str, dict[str, Any]], None]

# Accepted spellings -> canonical backend name (the BackendCapabilities.name
# and the id prefix in multiplexed deployments).
BACKEND_ALIASES: dict[str, str] = {
    "lambda": "lambda_labs",
    "lambdalabs": "lambda_labs",
    "thunder": "thunder_compute",
    "thundercompute": "thunder_compute",
    "datacrunch": "verda",
    "voltagepark": "voltage_park",
}


def _canonical_backend_name(name: str) -> str:
    lowered = name.strip().lower()
    return BACKEND_ALIASES.get(lowered, lowered)


def _build_named_backend(
    *,
    name: str,
    repo_root: Path,
    activity: ActivityHook | None = None,
) -> SandboxBackend:
    if name == "fake":
        from .backends.fake import FakeSandboxBackend

        return FakeSandboxBackend()
    if name == "modal":
        from .backends.modal import build_modal_sandbox_backend

        return build_modal_sandbox_backend(
            repo_root=repo_root,
            activity=activity,
        )
    if name == "thunder_compute":
        from .backends.thunder_compute import build_thunder_compute_sandbox_backend

        return build_thunder_compute_sandbox_backend(repo_root=repo_root)
    if name == "lambda_labs":
        from .backends.lambda_labs import build_lambda_labs_sandbox_backend

        return build_lambda_labs_sandbox_backend(repo_root=repo_root)
    if name == "hyperstack":
        from .backends.hyperstack import build_hyperstack_sandbox_backend

        return build_hyperstack_sandbox_backend(repo_root=repo_root)
    if name == "digitalocean":
        from .backends.digitalocean import build_digitalocean_sandbox_backend

        return build_digitalocean_sandbox_backend(repo_root=repo_root)
    if name == "verda":
        from .backends.verda import build_verda_sandbox_backend

        return build_verda_sandbox_backend(repo_root=repo_root)
    raise BackendUnavailableError(f"unknown execution backend: {name}")


def build_sandbox_backend(
    *,
    repo_root: Path,
    name: str | None = None,
    activity: ActivityHook | None = None,
) -> SandboxBackend:
    """Select and construct the configured sandbox backend(s).

    Backend name comes from (in order): `name=` arg,
    `RESEARCH_PLUGIN_EXECUTION_BACKEND` env, or "lambda_labs" by default.
    `RESEARCH_PLUGIN_EXECUTION_BACKENDS` (comma-separated) configures several
    providers at once behind one MultiplexingSandboxBackend; the single-name
    env then selects the default among them. One configured backend keeps
    today's direct, prefix-free path.
    """
    if name is not None:
        return _build_named_backend(
            name=_canonical_backend_name(name), repo_root=repo_root, activity=activity
        )
    configured = list(
        dict.fromkeys(  # de-dupe, keep configured order
            _canonical_backend_name(part)
            for part in (
                os.environ.get("RESEARCH_PLUGIN_EXECUTION_BACKENDS") or ""
            ).split(",")
            if part.strip()
        )
    )
    single = _canonical_backend_name(
        os.environ.get("RESEARCH_PLUGIN_EXECUTION_BACKEND") or ""
    )
    if len(configured) <= 1:
        return _build_named_backend(
            name=configured[0] if configured else (single or "lambda_labs"),
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
    "BackendCapabilities",
    "BackendPermissionError",
    "BackendUnavailableError",
    "BackendValidationError",
    "CapacityUnavailableError",
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
