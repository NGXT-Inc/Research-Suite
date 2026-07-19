"""In-memory sandbox backend for SandboxService tests."""

from __future__ import annotations

import threading

from ...run_receipts import parse_runs_listing
from ...sync_dirs import (
    DEFAULT_DATA_DIR,
    remote_experiment_dir,
)
from ...transcript_wire import TRANSCRIPT_TAIL_DEFAULT
from ....sandbox_backend import (
    BackendCapabilities,
    BackendUnavailableError,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxBackendBase,
    SandboxRequest,
    TranscriptTail,
)


class FakeSandboxBackend(SandboxBackendBase):
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

    def __init__(
        self,
        *,
        requires_hardware_selection: bool = False,
        configurable_resources: bool = True,
        catalog_options: list[dict] | None = None,
        catalog_regions: list[str] | None = None,
    ) -> None:
        self.capabilities = BackendCapabilities(
            name="fake",
            enforce_expiry=False,
            lifetime_extension_supported=True,
            requires_hardware_selection=requires_hardware_selection,
            configurable_resources=configurable_resources,
        )
        # Bundled-hardware (Lambda-style) selection menu. Off by default so the
        # backend stays a composable-resources stand-in for Modal; opt in to
        # exercise the needs_selection / sandbox.options flow without a cloud.
        # Gated: only when selection is required do we expose ``hardware_catalog``
        # at all, so default instances behave exactly as before (no catalog).
        self._catalog_options = catalog_options
        self._catalog_regions = catalog_regions
        if requires_hardware_selection:
            self.hardware_catalog = self._hardware_catalog_impl  # type: ignore[assignment]
        self.counter = 0
        self.acquired: list[SandboxRequest] = []
        self.alive: dict[str, bool] = {}
        self.terminated: list[str] = []
        self.transcripts: dict[str, str] = {}
        # Kwargs of every read_transcript call, so tests can assert the
        # registry hands backends the stored SSH endpoint + key.
        self.transcript_reads: list[dict] = []
        # Captured bootstrap content per sandbox id: which public keys the boot
        # would authorize — the fake's stand-in for Modal's BOOT_SCRIPT env and
        # Lambda's user_data.
        self.bootstraps: dict[str, dict] = {}
        self.remote_envs: dict[str, str] = {}
        self.by_experiment: dict[str, str] = {}
        # Live SSH endpoint per sandbox id; move_endpoint() simulates a tunnel
        # that Modal relocated so refresh_ssh_endpoint() can be exercised.
        self.endpoints: dict[str, tuple[str, int]] = {}
        self.phases: list[tuple[str, str]] = []
        self.healthy = True
        # async-path knobs
        self.gate: threading.Event | None = None
        self.fail_after_create = False
        self.fail_immediately = False
        # metrics knob: per-sandbox-id sample dict (None => unavailable).
        self.metrics: dict[str, dict | None] = {}
        # merv_run receipts knob: per-sandbox-id RAW listing text, exactly as the
        # on-box listing command would emit it — read_runs parses it with the
        # real wire parser so tests cover the whole observation path.
        self.run_listings: dict[str, str] = {}

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        self.acquired.append(request)
        if on_phase is not None:
            on_phase("creating", f"gpu={request.gpu or 'cpu'}")
            self.phases.append(("creating", request.experiment_id))
        if self.fail_immediately:
            raise BackendUnavailableError("fake create failure")
        self.counter += 1
        sandbox_id = f"sb-{self.counter}"
        name_key = request.sandbox_uid or request.experiment_id
        name = f"rp-{name_key}"
        self.alive[sandbox_id] = True
        self.by_experiment[name_key] = sandbox_id
        self.endpoints[sandbox_id] = ("sandbox.modal.test", 40000 + self.counter)
        # What a real bootstrap would do with this request: authorize BOTH
        # keys (user + management, plan Phase 5) and pre-install /opt/merv
        # tooling. The keys come straight from the captured request so a test
        # can assert exactly what reached the VM.
        self.bootstraps[sandbox_id] = {
            "authorized_keys": [
                key
                for key in (request.public_key, request.management_public_key)
                if key
            ],
        }
        workdir = request.remote_workdir or remote_experiment_dir(
            experiment_id=request.experiment_id
        )
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
        host, port = self.endpoints[sandbox_id]
        return ProvisionedSandbox(
            sandbox_id=sandbox_id,
            ssh_host=host,
            ssh_port=port,
            ssh_user="root",
            workdir=workdir,
            volume_name="",
            sync_dir=workdir,
            unsynced_dir=DEFAULT_DATA_DIR,
            sandbox_data_dir=DEFAULT_DATA_DIR,
            reused=False,
            gpu=request.gpu or "",
            instance_type=request.instance_type or "",
            region=request.region or "",
            # Cloud plan Phase 7: quote the catalog price for the chosen SKU so
            # the price-recording path is exercised in-process. Mirrors Lambda;
            # 0 when no instance_type / no matching catalog option (Modal-like).
            price_usd_per_hour=self._price_for(request.instance_type),
        )

    def _price_for(self, instance_type: str | None) -> float:
        """Catalog price for an instance_type (cloud plan Phase 7), or 0.

        Reads the same option list the selection menu exposes, so a test that
        requests a known SKU gets a non-zero price plumbed through to the row +
        the sandbox_generations ledger without a cloud.
        """
        if not instance_type:
            return 0.0
        options = (
            self._default_catalog_options()
            if self._catalog_options is None
            else self._catalog_options
        )
        for option in options:
            if str(option.get("instance_type") or "") == instance_type:
                return float(option.get("price_usd_per_hour") or 0.0)
        return 0.0

    def refresh_ssh_endpoint(self, *, sandbox_id: str) -> tuple[str, int] | None:
        if not self.alive.get(sandbox_id):
            return None
        return self.endpoints.get(sandbox_id)

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        sandbox_id = self.by_experiment.get(sandbox_uid or experiment_id)
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
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> TranscriptTail:
        self.transcript_reads.append(
            {
                "sandbox_id": sandbox_id,
                "experiment_id": experiment_id,
                "workdir": workdir,
                "ssh_host": ssh_host,
                "ssh_port": ssh_port,
                "ssh_user": ssh_user,
                "key_path": key_path,
            }
        )
        # Mirror the real backends: a bounded tail window plus the true total,
        # so service tests can exercise cursor math past the window.
        data = self.transcripts.get(experiment_id, "").encode("utf-8")
        total = len(data)
        limit = int(tail) if tail and tail > 0 else TRANSCRIPT_TAIL_DEFAULT
        if len(data) > limit:
            data = data[-limit:]
        return TranscriptTail(data=data, total_bytes=total)

    def sample_metrics(
        self,
        *,
        sandbox_id: str,
        ssh_host: str = "",  # noqa: ARG002
        ssh_port: int = 0,  # noqa: ARG002
        ssh_user: str = "",  # noqa: ARG002
        key_path: str = "",  # noqa: ARG002
    ) -> dict | None:
        if not self.alive.get(sandbox_id):
            return None
        return self.metrics.get(sandbox_id)

    def read_runs(
        self,
        *,
        sandbox_id: str,
        workdir: str = "",  # noqa: ARG002
        ssh_host: str = "",  # noqa: ARG002
        ssh_port: int = 0,  # noqa: ARG002
        ssh_user: str = "",  # noqa: ARG002
        key_path: str = "",  # noqa: ARG002
    ) -> list[dict] | None:
        if not self.alive.get(sandbox_id):
            return None
        return parse_runs_listing(self.run_listings.get(sandbox_id, ""))

    def sandbox_environment(self) -> dict:
        return {"available_tokens": [], "notes": []}

    def health(self) -> dict:
        return {"ok": self.healthy, "name": self.capabilities.name}

    # ---- bundled-hardware selection (opt-in; mirrors Lambda Labs) ----

    def _default_catalog_options(self) -> list[dict]:
        """A small, deterministic, cheapest-first SKU menu (Lambda-shaped)."""
        return [
            {"instance_type": "gpu_1x_a10", "gpu": "A10", "gpu_count": 1,
             "vcpus": 30, "memory_gib": 200, "storage_gib": 1400,
             "price_usd_per_hour": 0.75, "regions": ["us-west-1"], "available": True},
            {"instance_type": "gpu_1x_a100", "gpu": "A100", "gpu_count": 1,
             "vcpus": 30, "memory_gib": 200, "storage_gib": 1024,
             "price_usd_per_hour": 1.29, "regions": ["us-east-1", "us-west-1"],
             "available": True},
            {"instance_type": "gpu_8x_h100", "gpu": "H100", "gpu_count": 8,
             "vcpus": 208, "memory_gib": 1800, "storage_gib": 26000,
             "price_usd_per_hour": 23.92, "regions": ["us-east-1"], "available": True},
        ]

    def _hardware_catalog_impl(self, *, gpu: str | None = None, region: str | None = None) -> dict:
        """Filterable, cheapest-first menu — only bound when selection is on."""
        options = (
            self._default_catalog_options()
            if self._catalog_options is None
            else [dict(option) for option in self._catalog_options]
        )
        if gpu:
            needle = str(gpu).strip().upper()
            options = [o for o in options if needle in str(o.get("gpu", "")).upper()]
        if region:
            needle = str(region).strip().lower()
            options = [
                o for o in options
                if needle in [str(r).lower() for r in o.get("regions", [])]
            ]
        options.sort(key=lambda o: (o.get("price_usd_per_hour", 0.0), o.get("instance_type") or ""))
        regions = (
            list(self._catalog_regions)
            if self._catalog_regions is not None
            else sorted({r for o in options for r in o.get("regions", [])})
        )
        return self._selection_catalog(
            reason=(
                "Fake bundled-hardware backend: GPU+CPU+RAM ship as fixed machine "
                "types — pick one instance_type (mirrors Lambda Labs)."
            ),
            regions=regions,
            options=options,
        )

    # ---- test helpers ----

    def kill(self, *, sandbox_id: str) -> None:
        """Simulate Modal reaping a sandbox (timeout / crash)."""
        self.alive[sandbox_id] = False

    def move_endpoint(self, *, sandbox_id: str, host: str, port: int) -> None:
        """Simulate Modal relocating a live sandbox's SSH tunnel."""
        self.endpoints[sandbox_id] = (host, port)

    def append_transcript(self, *, experiment_id: str, text: str) -> None:
        self.transcripts[experiment_id] = self.transcripts.get(experiment_id, "") + text
