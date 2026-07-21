"""Composition-owned Sandbox collaborators and background runtime."""

from __future__ import annotations

from dataclasses import dataclass

from ..kernel.env import env_float
from ..kernel.ports.mgmt_keys import MgmtKeyStore
from ..kernel.ports.task_channel import TaskChannel
from ..kernel.state.store import BaseStateStore
from .repository import SandboxRepository
from .sandbox_backend import SandboxBackend
from .sandbox_daemons import SandboxDaemons
from .sandbox_lifecycle import SandboxLifecycle
from .sandbox_metrics import SandboxMetrics
from .sandbox_provisioner import SandboxProvisioner
from .sandbox_runs import SandboxRunLedger
from .sandbox_support import DEFAULT_STALE_PROVISION_SECONDS
from .transcript_cache import TranscriptCache


@dataclass(frozen=True, slots=True)
class SandboxRuntime:
    repository: SandboxRepository
    metrics: SandboxMetrics
    runs: SandboxRunLedger
    lifecycle: SandboxLifecycle
    provisioner: SandboxProvisioner
    daemons: SandboxDaemons
    transcripts: TranscriptCache

    def start(self) -> None:
        self.daemons.start()

    def shutdown(self) -> None:
        self.daemons.stop()
        self.provisioner.shutdown()


def build_sandbox_runtime(
    *,
    store: BaseStateStore,
    backend: SandboxBackend,
    mgmt_keys: MgmtKeyStore,
    tasks: TaskChannel,
    stale_provision_seconds: float | None = None,
    force_expiry_reaper: bool = False,
) -> SandboxRuntime:
    """Construct the runtime without starting any thread."""
    repository = SandboxRepository(store=store)
    metrics = SandboxMetrics(registry=repository, backend=backend, mgmt_keys=mgmt_keys)
    runs = SandboxRunLedger(
        store=store,
        registry=repository,
        backend=backend,
        mgmt_keys=mgmt_keys,
    )
    lifecycle = SandboxLifecycle(
        registry=repository,
        backend=backend,
        mgmt_keys=mgmt_keys,
        tasks=tasks,
    )
    provisioner = SandboxProvisioner(
        registry=repository,
        backend=backend,
        lifecycle=lifecycle,
        stale_provision_seconds=env_float(
            "RESEARCH_PLUGIN_SANDBOX_STALE",
            stale_provision_seconds,
            DEFAULT_STALE_PROVISION_SECONDS,
        ),
    )
    lifecycle.job_probe = provisioner.job_is_live
    daemons = SandboxDaemons(
        registry=repository,
        backend=backend,
        provisioner=provisioner,
        lifecycle=lifecycle,
        sample_metrics=metrics.sample_metrics,
        reconcile_runs=runs.reconcile_live,
        force_expiry_reaper=force_expiry_reaper,
    )
    return SandboxRuntime(
        repository=repository,
        metrics=metrics,
        runs=runs,
        lifecycle=lifecycle,
        provisioner=provisioner,
        daemons=daemons,
        transcripts=TranscriptCache(),
    )


__all__ = ["SandboxRuntime", "build_sandbox_runtime"]
