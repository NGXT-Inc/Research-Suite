"""DigitalOcean GPU Droplet sandbox backend.

Provisions a droplet (root SSH + public IPv4 by default) and returns SSH
details to the agent. Powered-off droplets still bill — destroy is the only
way to stop charges. GPU sizes stay hidden until DigitalOcean unlocks GPU
access for the account, and the default image is the AI/ML-ready Ubuntu
(drivers preinstalled); no A100 SKUs — the fleet is H100/H200/L40S/RTX-class.
"""

from __future__ import annotations

from contextlib import suppress
import time
from pathlib import Path
from typing import Any

from ...bootstrap_tools import BASELINE_APT_PACKAGES, ML_PYTHON_PACKAGES
from ...vm_bootstrap import build_standard_user_data
from ....sandbox_backend import (
    BackendCapabilities,
    BackendUnavailableError,
    BackendValidationError,
    CapacityUnavailableError,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxRequest,
)
from ....sandbox_paths import remote_experiment_dir, remote_root_of, remote_sessions_dir
from ..vm_ssh_backend import SshInputRunner, SshRunner, VmSshSandboxBackend, _vm_name as _sandbox_name
from .catalog import find_option, to_agent_options
from .client import DigitalOceanClient
from .config import DigitalOceanSandboxConfig


ACTIVE_DROPLET_STATUSES = frozenset({"active"})
# "off" droplets still bill; only "archive" (and 404) mean gone.
LIVE_DROPLET_STATUSES = frozenset({"new", "active", "off"})

# The droplet API rejects user_data over 64 KiB.
USER_DATA_MAX_BYTES = 64 * 1024

DIGITALOCEAN_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)


class DigitalOceanSandboxBackend(VmSshSandboxBackend):
    capabilities = BackendCapabilities(
        name="digitalocean",
        lifetime_extension_supported=True,
        requires_hardware_selection=True,
        configurable_resources=False,
    )

    def __init__(
        self,
        *,
        config: DigitalOceanSandboxConfig | None = None,
        client: DigitalOceanClient | None = None,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
    ) -> None:
        super().__init__(ssh_runner=ssh_runner, ssh_input_runner=ssh_input_runner)
        self._config = config
        self._client = client

    @property
    def config(self) -> DigitalOceanSandboxConfig:
        if self._config is None:
            self._config = DigitalOceanSandboxConfig.from_env()
        return self._config

    @property
    def client(self) -> DigitalOceanClient:
        if self._client is None:
            self._client = DigitalOceanClient(config=self.config.cloud)
        return self._client

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        droplet_name = _sandbox_name(request.sandbox_uid or request.experiment_id)
        size = (request.instance_type or self.config.size or "").strip()
        if not size:
            raise BackendValidationError(
                "DigitalOcean requires an instance_type (a GPU droplet size slug). "
                "Call sandbox.options, or sandbox.request without an instance_type, "
                "to see the available GPU sizes, then pick one."
            )
        self._notify(on_phase, "checking_capacity", size)
        option, region = self._resolve_placement(
            size=size,
            region=(request.region or self.config.region or "").strip(),
            requested_gpu=request.gpu,
        )

        key_id: int | str = ""
        droplet_id = ""
        try:
            self._notify(on_phase, "registering_ssh_key", f"{droplet_name}-key")
            key_id = self._ensure_ssh_key(
                name=f"{droplet_name}-key", public_key=request.public_key
            )

            self._notify(on_phase, "creating", f"{size} in {region}")
            workdir = request.remote_workdir or remote_experiment_dir(
                experiment_id=request.experiment_id, root=self.config.remote_root
            )
            user_data = build_standard_user_data(
                public_key=request.public_key,
                experiment_id=request.experiment_id,
                workdir=workdir,
                sessions_dir=remote_sessions_dir(
                    experiment_id=request.experiment_id, root=remote_root_of(workdir)
                ),
                sandbox_data_dir=self.config.sandbox_data_dir,
                management_public_key=request.management_public_key,
                apt_packages=DIGITALOCEAN_APT_PACKAGES,
                python_packages=ML_PYTHON_PACKAGES,
            )
            if len(user_data.encode("utf-8")) > USER_DATA_MAX_BYTES:
                raise BackendValidationError(
                    "DigitalOcean user_data exceeds the 64 KiB droplet limit"
                )
            droplet = self.client.create_droplet(
                name=droplet_name,
                region=region,
                size=size,
                image=self.config.image,
                ssh_key_ids=[key_id],
                user_data=user_data,
            )
            droplet_id = str(droplet["id"])
            self._notify(on_created, droplet_id, droplet_name)

            self._notify(on_phase, "connecting", "waiting for active droplet and ssh")
            droplet = self._wait_for_active_droplet(droplet_id=droplet_id)
            ip = _public_ipv4(droplet)
            if not ip:
                raise BackendUnavailableError(
                    "DigitalOcean droplet became active without a public IPv4"
                )
            self._wait_for_ssh(host=ip)
            return ProvisionedSandbox(
                sandbox_id=droplet_id,
                ssh_host=ip,
                ssh_port=22,
                ssh_user=self.config.ssh_user,
                workdir=workdir,
                volume_name="",
                sync_dir=workdir,
                unsynced_dir=self.config.sandbox_data_dir,
                sandbox_data_dir=self.config.sandbox_data_dir,
                reused=False,
                gpu=str(option.get("gpu") or request.gpu or ""),
                cpu=float(option.get("vcpus") or 0) or None,
                memory=(int(option.get("memory_gib") or 0) * 1024) or None,
                instance_type=size,
                region=region,
                price_usd_per_hour=float(option.get("price_usd_per_hour") or 0.0),
            )
        except Exception:
            if droplet_id:
                with suppress(Exception):
                    self.client.delete_droplet(droplet_id)
            if key_id:
                with suppress(Exception):
                    self.client.delete_ssh_key(key_id)
            raise

    def is_alive(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            droplet = self.client.get_droplet(sandbox_id)
        except BackendUnavailableError as exc:
            if exc.status == 404:
                return False  # authoritative: the droplet no longer exists
            raise  # outage/timeout — callers must not read this as "gone"
        return str(droplet.get("status") or "") in LIVE_DROPLET_STATUSES

    def terminate(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        key_ids = self._ssh_key_ids_for_droplet(sandbox_id=sandbox_id)
        try:
            self.client.delete_droplet(sandbox_id)
        except BackendUnavailableError as exc:
            if exc.status != 404:  # 404 = already destroyed; that IS terminated
                return False
        except Exception:  # noqa: BLE001
            return False
        for key_id in key_ids:
            with suppress(Exception):
                self.client.delete_ssh_key(key_id)
        return True

    def health(self) -> dict:
        return self._probe_health(lambda: self.client.list_sizes())

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        name = _sandbox_name(sandbox_uid or experiment_id)
        try:
            for droplet in self.client.list_droplets():
                if (
                    droplet.get("name") == name
                    and str(droplet.get("status") or "") in LIVE_DROPLET_STATUSES
                ):
                    return str(droplet.get("id") or "") or None
        except Exception:  # noqa: BLE001
            return None
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Menu of the GPU droplet sizes the account can currently see."""
        options = to_agent_options(
            self.client.list_sizes(), gpu=gpu, region=region, only_available=True
        )
        reason = (
            "DigitalOcean GPU droplets bundle GPU, CPU, and RAM into fixed size "
            "slugs; pick one instance_type. Destroyed droplets stop billing — "
            "powered-off ones do not."
        )
        if not options:
            reason += (
                " No GPU sizes are visible to this account: GPU droplet access "
                "usually needs a one-time unlock — request it in the DigitalOcean "
                "console under Create > GPU Droplets."
            )
        return self._selection_catalog(
            reason=reason,
            options=options,
        )

    def _resolve_placement(
        self, *, size: str, region: str, requested_gpu: str | None
    ) -> tuple[dict[str, Any], str]:
        """Validate the size + pick a region with the size on offer."""
        options = to_agent_options(self.client.list_sizes(), only_available=False)
        option = find_option(options, instance_type=size)
        if option is None:
            offered = ", ".join(sorted(o["instance_type"] for o in options)) or (
                "(none visible — the account may need the GPU droplet unlock)"
            )
            raise BackendValidationError(
                f"DigitalOcean size is not available to this account: {size}. "
                f"GPU sizes visible now: {offered}."
            )
        if requested_gpu and requested_gpu.upper() not in str(
            option.get("gpu_description") or ""
        ).upper() and requested_gpu.upper() != str(option.get("gpu") or "").upper():
            raise BackendValidationError(
                f"requested gpu {requested_gpu} does not match DigitalOcean size "
                f"{size} ({option.get('gpu_description') or 'unknown GPU'})"
            )
        available_regions = sorted(str(r) for r in option.get("regions", []))
        if region:
            if region not in available_regions:
                where = ", ".join(available_regions) or "(no regions)"
                raise CapacityUnavailableError(
                    f"DigitalOcean size {size} is not offered in {region}. "
                    f"Regions offering it now: {where}."
                )
            chosen = region
        else:
            if not available_regions or not option.get("available"):
                raise CapacityUnavailableError(
                    f"DigitalOcean size {size} has no availability right now. "
                    "Call sandbox.options to pick an available size."
                )
            chosen = available_regions[0]
        return option, chosen

    def _ensure_ssh_key(self, *, name: str, public_key: str) -> int | str:
        """Register the caller key; reuse the account's copy when it exists.

        DigitalOcean dedupes keys by fingerprint (422 on re-upload), so a
        re-registered caller key resolves to the already-stored id.
        """
        try:
            return self.client.create_ssh_key(name=name, public_key=public_key)["id"]
        except BackendUnavailableError as exc:
            if exc.status != 422:
                raise
        wanted = " ".join(public_key.split()[:2])
        for key in self.client.list_ssh_keys():
            stored = " ".join(str(key.get("public_key") or "").split()[:2])
            if stored == wanted and key.get("id"):
                return key["id"]
        raise BackendUnavailableError(
            "DigitalOcean rejected the SSH key as a duplicate but no matching "
            "stored key was found"
        )

    def _wait_for_active_droplet(self, *, droplet_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_status = ""
        while time.monotonic() < deadline:
            droplet = self.client.get_droplet(droplet_id)
            last_status = str(droplet.get("status") or "")
            if last_status in ACTIVE_DROPLET_STATUSES and _public_ipv4(droplet):
                return droplet
            if last_status == "archive":
                raise BackendUnavailableError(
                    f"DigitalOcean droplet {droplet_id} reached terminal status archive"
                )
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(
            f"DigitalOcean droplet {droplet_id} did not become active before timeout "
            f"(last status: {last_status or 'unknown'})"
        )

    def _ssh_key_ids_for_droplet(self, *, sandbox_id: str) -> list[int | str]:
        """The rp-named key registered for this droplet, resolved by name."""
        try:
            droplet = self.client.get_droplet(sandbox_id)
        except Exception:  # noqa: BLE001
            return []
        name = f"{droplet.get('name')}-key"
        if not str(droplet.get("name") or "").startswith("rp-"):
            return []
        try:
            keys = self.client.list_ssh_keys()
        except Exception:  # noqa: BLE001
            return []
        return [key["id"] for key in keys if key.get("name") == name and key.get("id")]


def _public_ipv4(droplet: dict[str, Any]) -> str:
    networks = droplet.get("networks")
    v4 = networks.get("v4") if isinstance(networks, dict) else None
    for entry in v4 or []:
        if isinstance(entry, dict) and entry.get("type") == "public":
            return str(entry.get("ip_address") or "")
    return ""


def build_digitalocean_sandbox_backend(
    *, repo_root: Path | None = None, **_kwargs: Any
) -> DigitalOceanSandboxBackend:
    # Lazy: the token resolves at call time, not construction.
    return DigitalOceanSandboxBackend()
