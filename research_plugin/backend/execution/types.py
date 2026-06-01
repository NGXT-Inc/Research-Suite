"""Backend-neutral sandbox-execution types and the SandboxBackend protocol.

Everything in this module is dependency-free with respect to the rest of the
app: backends and the SandboxService talk only through these contracts.

The execution model is sandbox-centric, not job-centric. The registry asks a
backend to *acquire* a live sandbox wired for SSH, *check* whether it is still
alive, *terminate* it, and *read* its terminal transcript. The agent runs
commands over SSH itself — the backend never wraps or queues commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


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
    backend authorizes inside the sandbox. `remote_workdir` is filled from the
    backend's default when left empty.
    """

    experiment_id: str
    project_id: str
    public_key: str
    gpu: str | None = None
    cpu: float = 2.0
    memory: int = 8192
    time_limit: int = 3600
    image_packages: tuple[str, ...] = ()
    cuda_devel: bool = False
    remote_workdir: str = ""


@dataclass(frozen=True)
class ProvisionedSandbox:
    """SSH connection facts for a live sandbox.

    Timing/persistence (expires_at, key_path, …) live in the registry, not here.
    """

    sandbox_id: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    workdir: str
    volume_name: str
    reused: bool = False


@dataclass(frozen=True)
class BackendCapabilities:
    name: str


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
    ) -> str: ...

    def health(self) -> dict: ...
