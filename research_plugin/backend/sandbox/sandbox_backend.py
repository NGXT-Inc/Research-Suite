"""Provider-neutral sandbox backend port, request types, and errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


SANDBOX_STATES = ("provisioning", "running", "terminated", "failed", "unknown")


class ExecutionBackendError(Exception):
    """Base error for sandbox backends.

    Carries an optional ``details`` dict so a backend error can attach a
    machine-readable reason (for example ``daemon_unreachable``) without
    forcing every raise site to populate it.
    """

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class BackendValidationError(ExecutionBackendError):
    """Caller-supplied sandbox spec or backend hints are malformed."""


class BackendPermissionError(ExecutionBackendError):
    """Caller-supplied sandbox spec or environment violates execution policy."""


class BackendUnavailableError(ExecutionBackendError):
    """The selected backend cannot be reached or initialized."""


# Provisioning progress callbacks. The registry passes these to acquire() so it
# can persist the sandbox id the instant it exists, before the slow SSH wait.
OnPhase = Callable[[str, str], None]      # (phase, detail)
OnCreated = Callable[[str, str], None]    # (sandbox_id, sandbox_name)


@dataclass(frozen=True)
class SandboxRequest:
    """A request to procure one SSH-reachable sandbox."""

    experiment_id: str
    project_id: str
    public_key: str
    sandbox_uid: str = ""
    management_public_key: str = ""
    management_key_path: str = ""
    gpu: str | None = None
    cpu: float = 2.0
    memory: int = 8192
    time_limit: int = 3600
    image_packages: tuple[str, ...] = ()
    cuda_devel: bool = False
    remote_workdir: str = ""
    instance_type: str | None = None
    region: str | None = None
    # Backend-owned experiment tracking variables that commands should inherit.
    # Used for centralized MLflow; providers persist these into /opt/rp/env.
    tracking_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProvisionedSandbox:
    """SSH connection facts for a live sandbox."""

    sandbox_id: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    workdir: str
    volume_name: str
    sync_dir: str = ""
    unsynced_dir: str = ""
    sandbox_data_dir: str = ""
    reused: bool = False
    dashboards: Mapping[str, str] = field(default_factory=dict)
    gpu: str = ""
    cpu: float | None = None
    memory: int | None = None
    instance_type: str = ""
    region: str = ""
    price_usd_per_hour: float = 0.0


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    # A provider that forgets this flag gets billing protection by default.
    enforce_expiry: bool = True
    # True when a provider-bundled machine SKU must be selected first.
    requires_hardware_selection: bool = False
    # True when cpu/memory/gpu can be requested independently.
    configurable_resources: bool = True


class SandboxBackend(Protocol):
    capabilities: BackendCapabilities

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox: ...

    def is_alive(self, *, sandbox_id: str) -> bool: ...

    def terminate(self, *, sandbox_id: str) -> bool: ...

    def read_transcript(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        volume_name: str,
        workdir: str,
        tail: int | None = None,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> str: ...

    def sandbox_environment(self) -> dict: ...

    def health(self) -> dict: ...

    def sample_metrics(
        self,
        *,
        sandbox_id: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> dict[str, Any] | None:
        """Optionally sample live sandbox usage. Unsupported backends return None."""
        ...

    def refresh_ssh_endpoint(self, *, sandbox_id: str) -> tuple[str, int] | None:
        """Optionally refresh a live SSH endpoint. Unsupported backends return None."""
        ...

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any] | None:
        """Optionally return requestable hardware. Unsupported backends return None."""
        ...

    def dashboard_urls(self, *, sandbox_id: str) -> dict[str, str] | None:
        """Optionally refresh provider-native dashboard URLs. Unsupported backends return None."""
        ...

    def local_dashboard_ports(self) -> dict[str, int]:
        """Optionally expose dashboards through local SSH forwards. Unsupported backends return {}."""
        ...

    def find_sandbox_id(self, *, experiment_id: str, sandbox_uid: str = "") -> str | None:
        """Optionally find an orphan sandbox by experiment. Unsupported backends return None."""
        ...

    def sandbox_secrets(self) -> dict[str, str]:
        """Optionally return post-boot secrets for the backend."""
        ...

    def write_secrets(
        self,
        *,
        sandbox_id: str,
        secrets: Mapping[str, str],
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> bool:
        """Optionally deliver provider credentials post-boot."""
        ...

    def retarget(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        public_key: str,
        workdir: str,
        sandbox_data_dir: str,
        tracking_env: Mapping[str, str],
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> bool:
        """Optionally point a live sandbox at another experiment."""
        ...

    def shutdown(self) -> None:
        """Optionally release backend-level resources. Unsupported backends no-op."""
        ...


class SandboxBackendBase:
    """Sentinel defaults for optional SandboxBackend operations."""

    def sample_metrics(
        self,
        *,
        sandbox_id: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> dict[str, Any] | None:
        """Unsupported default: no live usage sample is available."""
        return None

    def refresh_ssh_endpoint(self, *, sandbox_id: str) -> tuple[str, int] | None:
        """Unsupported default: no refreshed SSH endpoint is available."""
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any] | None:
        """Unsupported default: no hardware catalog is available."""
        return None

    def dashboard_urls(self, *, sandbox_id: str) -> dict[str, str] | None:
        """Unsupported default: no provider-native dashboard URLs are available."""
        return None

    def local_dashboard_ports(self) -> dict[str, int]:
        """Unsupported default: no dashboards need local SSH forwarding."""
        return {}

    def find_sandbox_id(self, *, experiment_id: str, sandbox_uid: str = "") -> str | None:
        """Unsupported default: no orphan lookup is available."""
        return None

    def sandbox_secrets(self) -> dict[str, str]:
        """Unsupported default: no post-boot secrets to deliver."""
        return {}

    def write_secrets(
        self,
        *,
        sandbox_id: str,
        secrets: Mapping[str, str],
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> bool:
        """Unsupported default: no post-boot secret channel."""
        return False

    def retarget(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        public_key: str,
        workdir: str,
        sandbox_data_dir: str,
        tracking_env: Mapping[str, str],
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> bool:
        """Unsupported default: this backend cannot reuse across experiments."""
        return False

    def shutdown(self) -> None:
        """Unsupported default: no backend-level resources need cleanup."""
        return None


__all__ = [
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
]
