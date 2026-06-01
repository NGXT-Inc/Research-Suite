"""In-memory sandbox backend for SandboxService tests."""

from __future__ import annotations

import threading

from ...errors import BackendUnavailableError
from ...types import (
    BackendCapabilities,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxRequest,
)


class FakeSandboxBackend:
    """Deterministic stand-in for ModalSandboxBackend.

    Tracks acquired sandboxes, liveness, terminations, and a per-experiment
    transcript so the registry's reuse/release/terminal logic can be exercised
    without Modal.

    Test knobs for the async provisioning path:
      - ``gate``: if set, ``acquire`` blocks at the "connecting" phase until the
        test sets the event — lets a test observe the `provisioning` state
        deterministically (no sleeps).
      - ``fail_after_create``: raise during the tunnel step (after the sandbox
        exists) to exercise failed-path cleanup / orphan termination.
      - ``fail_immediately``: raise before any sandbox is created.
    """

    def __init__(self) -> None:
        self.capabilities = BackendCapabilities(name="fake")
        self.counter = 0
        self.acquired: list[SandboxRequest] = []
        self.alive: dict[str, bool] = {}
        self.terminated: list[str] = []
        self.transcripts: dict[str, str] = {}
        self.by_experiment: dict[str, str] = {}
        self.phases: list[tuple[str, str]] = []
        self.healthy = True
        # async-path knobs
        self.gate: threading.Event | None = None
        self.fail_after_create = False
        self.fail_immediately = False

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        self.acquired.append(request)
        if on_phase is not None:
            on_phase("syncing", "pushing repo to volume")
            self.phases.append(("syncing", request.experiment_id))
        if self.fail_immediately:
            raise BackendUnavailableError("fake create failure")
        if on_phase is not None:
            on_phase("creating", f"gpu={request.gpu or 'cpu'}")
        self.counter += 1
        sandbox_id = f"sb-{self.counter}"
        name = f"rp-{request.experiment_id}"
        self.alive[sandbox_id] = True
        self.by_experiment[request.experiment_id] = sandbox_id
        workdir = request.remote_workdir or "/workspace/repo"
        # Past create: a failure must terminate the sandbox (mirrors Modal).
        try:
            if on_created is not None:
                on_created(sandbox_id, name)  # may raise to cancel
            if on_phase is not None:
                on_phase("connecting", "waiting for ssh")
            if self.gate is not None:
                self.gate.wait()
            if self.fail_after_create:
                raise BackendUnavailableError("fake tunnel failure")
        except BaseException:
            self.terminate(sandbox_id=sandbox_id)
            raise
        return ProvisionedSandbox(
            sandbox_id=sandbox_id,
            ssh_host="sandbox.modal.test",
            ssh_port=40000 + self.counter,
            ssh_user="root",
            workdir=workdir,
            volume_name=f"research-plugin-{request.project_id}",
            reused=False,
        )

    def find_sandbox_id(self, *, experiment_id: str) -> str | None:
        sandbox_id = self.by_experiment.get(experiment_id)
        if sandbox_id and self.alive.get(sandbox_id):
            return sandbox_id
        return None

    def is_alive(self, *, sandbox_id: str) -> bool:
        return bool(self.alive.get(sandbox_id, False))

    def terminate(self, *, sandbox_id: str) -> bool:
        self.alive[sandbox_id] = False
        self.terminated.append(sandbox_id)
        return True

    def read_transcript(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        volume_name: str,
        workdir: str,
        tail: int | None = None,
    ) -> str:
        text = self.transcripts.get(experiment_id, "")
        if tail and tail > 0 and len(text) > tail:
            return text[-tail:]
        return text

    def health(self) -> dict:
        return {"ok": self.healthy, "name": self.capabilities.name}

    # ---- test helpers ----

    def kill(self, *, sandbox_id: str) -> None:
        """Simulate Modal reaping a sandbox (timeout / crash)."""
        self.alive[sandbox_id] = False

    def append_transcript(self, *, experiment_id: str, text: str) -> None:
        self.transcripts[experiment_id] = self.transcripts.get(experiment_id, "") + text
