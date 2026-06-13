"""In-memory sandbox backend for SandboxService tests."""

from __future__ import annotations

import hashlib
import io
import tarfile
import threading
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from ...errors import BackendUnavailableError
from ...sync_dirs import DEFAULT_DATA_DIR, remote_experiment_dir
from ...transfer_spec import (
    build_parachute_script,
    is_excluded_relpath,
    max_size_bytes_for,
)
from ...types import (
    BackendCapabilities,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxBackendBase,
    SandboxRequest,
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
        default_dashboards: bool = True,
    ) -> None:
        self.capabilities = BackendCapabilities(
            name="fake",
            enforce_expiry=False,
            auto_sync=False,
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
        self.default_dashboards = default_dashboards
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
        # Captured bootstrap content per sandbox id (plan Phase 5): which
        # public keys the boot would authorize and which /opt/rp files it
        # would pre-install — the fake's stand-in for Modal's BOOT_SCRIPT env
        # and Lambda's user_data, so dual-key and parachute-install tests run
        # without a cloud.
        self.bootstraps: dict[str, dict] = {}
        # Management-channel exec/upload capture (plan Phase 5): every
        # run_parachute call lands here so the reaper's parachute branch is
        # observable in-process.
        self.parachute_calls: list[dict] = []
        # Simulated remote experiment dir per sandbox id: relpath → bytes.
        # Seeded by tests; run_parachute tars it per the shared transfer spec.
        self.remote_files: dict[str, dict[str, bytes]] = {}
        self.by_experiment: dict[str, str] = {}
        # Live SSH endpoint per sandbox id; move_endpoint() simulates a tunnel
        # that Modal relocated so refresh_ssh_endpoint() can be exercised.
        self.endpoints: dict[str, tuple[str, int]] = {}
        # Observability dashboard URLs per sandbox id, mirroring Modal's
        # encrypted-tunnel surface. Empty by default; tests opt in by setting
        # the entry or calling move_dashboards() to simulate a relocation.
        self.dashboards: dict[str, dict[str, str]] = {}
        self.phases: list[tuple[str, str]] = []
        self.healthy = True
        # async-path knobs
        self.gate: threading.Event | None = None
        self.fail_after_create = False
        self.fail_immediately = False
        # metrics knob: per-sandbox-id sample dict (None => unavailable).
        self.metrics: dict[str, dict | None] = {}

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
        name = f"rp-{request.experiment_id}"
        self.alive[sandbox_id] = True
        self.by_experiment[request.experiment_id] = sandbox_id
        self.endpoints[sandbox_id] = ("sandbox.modal.test", 40000 + self.counter)
        # What a real bootstrap would do with this request: authorize BOTH
        # keys (user + management, plan Phase 5) and pre-install /opt/rp
        # tooling. The keys come straight from the captured request so a test
        # can assert exactly what reached the VM.
        self.bootstraps[sandbox_id] = {
            "authorized_keys": [
                key
                for key in (request.public_key, request.management_public_key)
                if key
            ],
            "files": dict(self.bootstrap_files()),
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
        # Default fake-Modal dashboard URLs so the SandboxService persistence +
        # serializer path is exercised. A test wanting "no dashboards" can clear
        # ``self.dashboards[sandbox_id]``.
        if self.default_dashboards:
            self.dashboards.setdefault(
                sandbox_id,
                {
                    "mlflow": f"https://mlflow-{sandbox_id}.modal.test",
                    "tensorboard": f"https://tensorboard-{sandbox_id}.modal.test",
                },
            )
        else:
            self.dashboards.setdefault(sandbox_id, {})
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
            dashboards=dict(self.dashboards[sandbox_id]),
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

    def dashboard_urls(self, *, sandbox_id: str) -> dict[str, str]:
        if not self.alive.get(sandbox_id):
            return {}
        return dict(self.dashboards.get(sandbox_id, {}))

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
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> str:
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
        text = self.transcripts.get(experiment_id, "")
        if tail and tail > 0 and len(text) > tail:
            return text[-tail:]
        return text

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

    def bootstrap_files(self) -> dict[str, str]:
        """The /opt/rp files a real bootstrap pre-installs (path → content).

        Mirrors what Modal's file layer and Lambda's user_data ship, so a
        test can assert the parachute really rides along at bootstrap time.
        """
        return {"/opt/rp/parachute.sh": build_parachute_script()}

    def run_parachute(
        self,
        *,
        sandbox_id: str,
        put_url: str,
        ssh_host: str = "",  # noqa: ARG002 — the fake needs no endpoint
        ssh_port: int = 0,  # noqa: ARG002
        key_path: str = "",
    ) -> dict | None:
        """Simulate the VM-side parachute per the shared transfer spec.

        Tars the seeded ``remote_files`` honoring the SAME excludes and
        per-file size caps the real /opt/rp/parachute.sh enforces, and
        honors ``file://`` PUT targets (the local blob store's single-use
        staging URL). Any other scheme needs Phase 8's real presigned URLs.
        A dead sandbox has no channel to run anything.
        """
        self.parachute_calls.append(
            {"sandbox_id": sandbox_id, "put_url": put_url, "key_path": key_path}
        )
        if not self.alive.get(sandbox_id):
            raise BackendUnavailableError("fake sandbox is not alive")
        files = self.remote_files.get(sandbox_id) or {}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for relpath, content in sorted(files.items()):
                if is_excluded_relpath(relpath):
                    continue
                if len(content) > max_size_bytes_for(relpath):
                    continue
                info = tarfile.TarInfo(name=f"./{relpath}")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        data = buffer.getvalue()
        target = urlsplit(put_url)
        if target.scheme != "file":
            raise BackendUnavailableError(
                "fake parachute uploads only to file:// targets "
                "(real presigned URLs are Phase 8's S3)"
            )
        Path(url2pathname(target.path)).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

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
        return {
            "provider": "fake",
            "selection_required": self.capabilities.requires_hardware_selection,
            "select_with": "instance_type",
            "reason": (
                "Fake bundled-hardware backend: GPU+CPU+RAM ship as fixed machine "
                "types — pick one instance_type (mirrors Lambda Labs)."
            ),
            "regions": regions,
            "count": len(options),
            "options": options,
        }

    # ---- test helpers ----

    def kill(self, *, sandbox_id: str) -> None:
        """Simulate Modal reaping a sandbox (timeout / crash)."""
        self.alive[sandbox_id] = False

    def move_endpoint(self, *, sandbox_id: str, host: str, port: int) -> None:
        """Simulate Modal relocating a live sandbox's SSH tunnel."""
        self.endpoints[sandbox_id] = (host, port)

    def move_dashboards(self, *, sandbox_id: str, urls: dict[str, str]) -> None:
        """Simulate Modal relocating a live sandbox's encrypted dashboard tunnels."""
        self.dashboards[sandbox_id] = dict(urls)

    def append_transcript(self, *, experiment_id: str, text: str) -> None:
        self.transcripts[experiment_id] = self.transcripts.get(experiment_id, "") + text
