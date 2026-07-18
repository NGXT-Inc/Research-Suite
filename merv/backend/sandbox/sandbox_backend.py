"""Provider-neutral sandbox backend port, request types, and errors."""

from __future__ import annotations

from dataclasses import dataclass
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
    """The selected backend cannot be reached or initialized.

    ``status`` carries the HTTP status when the provider answered at all
    (None = no answer), so liveness checks can separate "instance gone"
    from "provider down".
    """

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.status = status


class CapacityUnavailableError(BackendUnavailableError):
    """The provider has no stock for the requested SKU/region right now.

    A routine, retryable condition — distinct from an outage: the provider
    answered, it just has nothing to sell. Callers surface it as a clear
    provision-failure reason and may retry another SKU, region, or provider.
    """


# Provisioning progress callbacks. The registry passes these to acquire() so it
# can persist the sandbox id the instant it exists, before the slow SSH wait.
OnPhase = Callable[[str, str], None]      # (phase, detail)
OnCreated = Callable[[str, str], None]    # (sandbox_id, sandbox_name)


@dataclass(frozen=True)
class TranscriptTail:
    """A bounded tail window of a sandbox transcript, plus its true size.

    ``data`` is the last ``len(data)`` bytes of the transcript; ``total_bytes``
    is the size of the whole transcript file, so callers can keep a cursor in
    absolute byte offsets even after the log outgrows the window. ``data``
    stays undecoded: offsets are raw bytes, and decoding (with replacement)
    before slicing would let multibyte characters skew the cursor math.
    """

    data: bytes = b""
    total_bytes: int = 0


@dataclass(frozen=True)
class SandboxRequest:
    """A request to procure one SSH-reachable sandbox."""

    experiment_id: str
    project_id: str
    public_key: str
    sandbox_uid: str = ""
    public_key_source: str = "managed"
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
    # Compute provider to serve this request when several are configured
    # (multiplexed deployments); None = the configured default backend.
    provider: str | None = None


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
    # True when the plugin's expires_at row is the effective lifetime control.
    # Modal's provider timeout is fixed at creation, so it stays false there.
    lifetime_extension_supported: bool = False
    # True when a provider-bundled machine SKU must be selected first.
    requires_hardware_selection: bool = False
    # True when cpu/memory/gpu can be requested independently.
    configurable_resources: bool = True


class SandboxBackend(Protocol):
    capabilities: BackendCapabilities

    def capabilities_for(self, *, provider: str | None = None) -> BackendCapabilities:
        """Capabilities of the backend that would serve ``provider``.

        Keyed by data value so services never branch on provider names; a
        single-provider backend ignores the argument and returns its own.
        """
        ...

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
    ) -> TranscriptTail: ...

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

    def read_runs(
        self,
        *,
        sandbox_id: str,
        workdir: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> list[dict[str, Any]] | None:
        """Optionally list merv_run receipts under workdir/.runs.

        Returns parsed run records ([] when no runs exist); None means
        unsupported or unreachable — "no news", never "no runs".
        """
        ...

    def refresh_ssh_endpoint(self, *, sandbox_id: str) -> tuple[str, int] | None:
        """Optionally refresh a live SSH endpoint. Unsupported backends return None."""
        ...

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any] | None:
        """Optionally return requestable hardware. Unsupported backends return None."""
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

    def shutdown(self) -> None:
        """Optionally release backend-level resources. Unsupported backends no-op."""
        ...


class SandboxBackendBase:
    """Sentinel defaults for optional SandboxBackend operations."""

    capabilities: BackendCapabilities

    def capabilities_for(self, *, provider: str | None = None) -> BackendCapabilities:
        """Single-provider default: one backend serves every request."""
        _ = provider
        return self.capabilities

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

    def read_runs(
        self,
        *,
        sandbox_id: str,
        workdir: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> list[dict[str, Any]] | None:
        """Unsupported default: no run receipts are observable."""
        return None

    def refresh_ssh_endpoint(self, *, sandbox_id: str) -> tuple[str, int] | None:
        """Unsupported default: no refreshed SSH endpoint is available."""
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any] | None:
        """Unsupported default: no hardware catalog is available."""
        return None

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

    def shutdown(self) -> None:
        """Unsupported default: no backend-level resources need cleanup."""
        return None


__all__ = [
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
    "TranscriptTail",
]
