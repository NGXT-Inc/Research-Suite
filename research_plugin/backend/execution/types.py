"""Backend-neutral sandbox-execution types and the SandboxBackend protocol.

Everything in this module is dependency-free with respect to the rest of the
app: backends and the SandboxService talk only through these contracts.

The execution model is sandbox-centric, not job-centric. The registry asks a
backend to *acquire* a live sandbox wired for SSH, *check* whether it is still
alive, *terminate* it, and *read* its terminal transcript. The agent runs
commands over SSH itself — the backend never wraps or queues commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


SANDBOX_STATES = ("provisioning", "running", "terminated", "failed", "unknown")

# Provisioning progress callbacks. The registry passes these to acquire() so it
# can persist the sandbox id the instant it exists (before the slow SSH-tunnel
# wait) and surface a live phase. Either callback may raise to abort the acquire
# cooperatively (cancellation); the backend then terminates anything it created
# before re-raising, so a canceled or failed acquire never leaks a sandbox.
OnPhase = Callable[[str, str], None]      # (phase, detail)
OnCreated = Callable[[str, str], None]    # (sandbox_id, sandbox_name)


@dataclass(frozen=True)
class SandboxRequest:
    """A request to procure a sandbox for one experiment.

    `public_key` is the registry-owned per-experiment SSH public key that the
    backend authorizes inside the sandbox; it is the *user* key — data-plane
    property (rsync, the sbx dispatcher, tunnels). `management_public_key` is
    the control-plane-minted management key (plan Phase 5, fixed decision 4),
    authorized at bootstrap alongside it so transcript reads, metrics
    sampling, and the expiry parachute never depend on the user's machine.
    Empty means "no management channel" (legacy callers). `remote_workdir` is
    the experiment's one synced folder on the VM; when left empty the backend
    derives `<remote_root>/<experiment_id>` from its config.

    Hardware selection is provider-shaped:

      - On a backend with *configurable* resources (Modal), `gpu`/`cpu`/`memory`
        are honored independently and `instance_type`/`region` are ignored.
      - On a backend that bundles hardware into fixed SKUs (Lambda Labs),
        `instance_type` picks the whole machine (GPU + vCPU + RAM together) and
        `region` optionally pins the datacenter; `cpu`/`memory` are advisory
        only (the SKU fixes them) and `gpu` acts as a filter/cross-check.
    """

    experiment_id: str
    project_id: str
    public_key: str
    management_public_key: str = ""
    gpu: str | None = None
    cpu: float = 2.0
    memory: int = 8192
    time_limit: int = 3600
    image_packages: tuple[str, ...] = ()
    cuda_devel: bool = False
    remote_workdir: str = ""
    # Provider-bundled hardware selection (Lambda Labs and similar). Empty/None
    # on configurable backends.
    instance_type: str | None = None
    region: str | None = None


@dataclass(frozen=True)
class ProvisionedSandbox:
    """SSH connection facts for a live sandbox.

    Timing/persistence (expires_at, key_path, …) live in the registry, not here.
    `dashboards` is a name → URL map for in-sandbox observability servers
    (MLflow at 5000, TensorBoard at 6006). Providers can return native URLs
    (Modal HTTPS tunnels), or the registry can fill local SSH-forward URLs for
    providers such as Lambda Labs. Empty when no dashboards are exposed. The map
    is immutable so the dataclass stays hashable.
    """

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
    # What the backend actually provisioned. Lets the registry record the real
    # reserved hardware for the UI/metrics framing even when the request did not
    # name it explicitly (e.g. Lambda resolves a fixed SKU's GPU/vCPU/RAM).
    # Empty/None means "use the request's values".
    gpu: str = ""
    cpu: float | None = None
    memory: int | None = None
    instance_type: str = ""
    region: str = ""
    # Provider price quote for the procured instance (cloud plan Phase 7, cost
    # governance). Lambda returns it from the catalog option; Modal has no
    # per-hour quote and leaves it 0. Recorded on the sandbox row + the
    # sandbox_generations ledger so spend is reconstructable.
    price_usd_per_hour: float = 0.0


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    # Run the expiration reaper for this backend's sandboxes. Default True:
    # a new provider that forgets this flag gets billing protection, not a
    # VM that bills forever. The in-memory fake opts out.
    enforce_expiry: bool = True
    # Run the background rsync poller. Default True: results preservation
    # is the safe default for any real remote sandbox.
    auto_sync: bool = True
    # True when the agent must pick a provider-bundled machine SKU (GPU + CPU +
    # RAM together) before a sandbox can be created, because the backend has no
    # configurable per-resource knobs. Lambda Labs sets this; Modal does not.
    requires_hardware_selection: bool = False
    # True when cpu/memory (and gpu) can be requested independently. Modal: yes;
    # Lambda Labs: no (the SKU fixes them).
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
        # The registry's stored SSH endpoint + per-experiment private key.
        # Backends that read the transcript over plain SSH (Lambda Labs) need
        # them; control-plane backends (Modal exec) ignore them.
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

    def find_sandbox_id(self, *, experiment_id: str) -> str | None:
        """Optionally find an orphan sandbox by experiment. Unsupported backends return None."""
        ...

    def run_parachute(
        self,
        *,
        sandbox_id: str,
        put_url: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> dict[str, Any] | None:
        """Optionally run the pre-installed expiry parachute (plan Phase 5).

        Executes ``/opt/rp/parachute.sh <put_url>`` over the backend's
        management channel (Modal: control-plane exec; Lambda: SSH with the
        management key in ``key_path``). Returns the parsed
        ``{sha256, size_bytes}`` upload receipt, or None when the backend has
        no parachute channel.
        """
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

    def find_sandbox_id(self, *, experiment_id: str) -> str | None:
        """Unsupported default: no orphan lookup is available."""
        return None

    def run_parachute(
        self,
        *,
        sandbox_id: str,
        put_url: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> dict[str, Any] | None:
        """Unsupported default: no parachute channel is available."""
        return None

    def shutdown(self) -> None:
        """Unsupported default: no backend-level resources need cleanup."""
        return None
