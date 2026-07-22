"""TensorDock VM sandbox backend.

Provisions a marketplace VM with a DEDICATED public IP (mandatory — the
catalog only offers dedicated-IP-capable locations; port-mapped hosts cannot
serve the direct-SSH contract) and returns SSH details to the agent.
Billing is per-second against a prepaid balance; there is no billing API, so
the provision-time price quote is the recorded rate, refined by the live
``rateHourly`` once the VM reports it.
"""

from __future__ import annotations

from contextlib import suppress
import time
from pathlib import Path
from typing import Any

from .._values import _float_or_zero
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
from .catalog import deploy_shape, find_option, parse_instance_type, to_agent_options
from .client import TensorDockClient
from .config import TensorDockSandboxConfig


ACTIVE_INSTANCE_STATUSES = frozenset({"running"})
# Statuses arrive in mixed casing ("running", "Stopped"); compare lowercased.
TERMINAL_INSTANCE_STATUSES = frozenset({"terminated", "deleted"})

TENSORDOCK_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)

BOOTSTRAP_PATH = "/opt/merv/bootstrap.sh"


class TensorDockSandboxBackend(VmSshSandboxBackend):
    capabilities = BackendCapabilities(
        name="tensordock",
        lifetime_extension_supported=True,
        requires_hardware_selection=True,
        configurable_resources=False,
    )

    def __init__(
        self,
        *,
        config: TensorDockSandboxConfig | None = None,
        client: TensorDockClient | None = None,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
    ) -> None:
        super().__init__(ssh_runner=ssh_runner, ssh_input_runner=ssh_input_runner)
        self._config = config
        self._client = client

    @property
    def config(self) -> TensorDockSandboxConfig:
        if self._config is None:
            self._config = TensorDockSandboxConfig.from_env()
        return self._config

    @property
    def client(self) -> TensorDockClient:
        if self._client is None:
            self._client = TensorDockClient(config=self.config.cloud)
        return self._client

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        vm_name = _sandbox_name(request.sandbox_uid or request.experiment_id)
        instance_type = (request.instance_type or "").strip()
        if not instance_type:
            raise BackendValidationError(
                "TensorDock requires an instance_type (e.g. 1x-h100-sxm5-80gb). "
                "Call sandbox.options, or sandbox.request without an instance_type, "
                "to see live availability, then pick one."
            )
        parsed = parse_instance_type(instance_type)
        if parsed is None:
            raise BackendValidationError(
                f"TensorDock instance_type must look like '<count>x-<gpu>' "
                f"(e.g. 1x-h100-sxm5-80gb), got: {instance_type}"
            )
        gpu_count, v0_name = parsed
        self._notify(on_phase, "checking_capacity", instance_type)
        option = self._resolve_option(
            instance_type=instance_type,
            region=(request.region or "").strip(),
            requested_gpu=request.gpu,
        )
        location_id = str((option.get("regions") or [""])[0])
        shape = deploy_shape(option)

        instance_id = ""
        try:
            self._notify(on_phase, "creating", f"{instance_type} in {location_id}")
            workdir = request.remote_workdir or remote_experiment_dir(
                experiment_id=request.experiment_id, root=self.config.remote_root
            )
            bootstrap = build_standard_user_data(
                public_key=request.public_key,
                experiment_id=request.experiment_id,
                workdir=workdir,
                sessions_dir=remote_sessions_dir(
                    experiment_id=request.experiment_id, root=remote_root_of(workdir)
                ),
                sandbox_data_dir=self.config.sandbox_data_dir,
                management_public_key=request.management_public_key,
                apt_packages=TENSORDOCK_APT_PACKAGES,
                python_packages=ML_PYTHON_PACKAGES,
            )
            instance = self.client.create_instance(
                name=vm_name,
                image=self.config.image,
                location_id=location_id,
                vcpu_count=shape["vcpu_count"],
                ram_gb=shape["ram_gb"],
                storage_gb=shape["storage_gb"],
                gpus={v0_name: {"count": gpu_count}},
                ssh_key=request.public_key,
                # The bootstrap authorizes root + the management principal;
                # cloud-init runs it as root on first boot.
                cloud_init=_bootstrap_cloud_init(bootstrap),
            )
            instance_id = str(instance.get("id") or "")
            self._notify(on_created, instance_id, vm_name)

            self._notify(on_phase, "connecting", "waiting for running VM and ssh")
            instance = self._wait_for_running_instance(instance_id=instance_id)
            host, port = _ssh_endpoint(instance)
            if not host:
                raise BackendUnavailableError(
                    "TensorDock VM is running without an IP address"
                )
            self._wait_for_ssh(host=host, port=port)
            return ProvisionedSandbox(
                sandbox_id=instance_id,
                ssh_host=host,
                ssh_port=port,
                ssh_user=self.config.ssh_user,
                workdir=workdir,
                volume_name="",
                sync_dir=workdir,
                unsynced_dir=self.config.sandbox_data_dir,
                sandbox_data_dir=self.config.sandbox_data_dir,
                reused=False,
                gpu=str(option.get("gpu") or request.gpu or ""),
                cpu=float(shape["vcpu_count"]),
                memory=shape["ram_gb"] * 1024,
                instance_type=instance_type,
                region=location_id,
                # Live rate once reported; the synthesized estimate otherwise.
                price_usd_per_hour=_float_or_zero(instance.get("rateHourly"))
                or float(option.get("price_usd_per_hour") or 0.0),
            )
        except Exception:
            if instance_id:
                with suppress(Exception):
                    self.client.delete_instance(instance_id)
            raise

    def is_alive(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            instance = self.client.get_instance(sandbox_id)
        except BackendUnavailableError as exc:
            if exc.status == 404:
                return False  # authoritative: the instance no longer exists
            raise  # outage/timeout — callers must not read this as "gone"
        return (
            str(instance.get("status") or "").lower() not in TERMINAL_INSTANCE_STATUSES
        )

    def terminate(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            self.client.delete_instance(sandbox_id)
        except BackendUnavailableError as exc:
            if exc.status != 404:  # 404 = already gone; that IS terminated
                return False
        except Exception:  # noqa: BLE001
            return False
        return True

    def health(self) -> dict:
        return self._probe_health(lambda: self.client.list_locations())

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        name = _sandbox_name(sandbox_uid or experiment_id)
        try:
            for instance in self.client.list_instances():
                attributes = (
                    instance.get("attributes")
                    if isinstance(instance.get("attributes"), dict)
                    else instance
                )
                if (
                    str(attributes.get("name") or "") == name
                    and str(attributes.get("status") or "").lower()
                    not in TERMINAL_INSTANCE_STATUSES
                ):
                    return str(instance.get("id") or "") or None
        except Exception:  # noqa: BLE001
            return None
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Menu of dedicated-IP-capable GPU shapes across marketplace hosts."""
        options = to_agent_options(
            self.client.list_locations(), gpu=gpu, region=region, only_available=True
        )
        return self._selection_catalog(
            reason=(
                "TensorDock composes machines per host; these options are "
                "synthesized GPU shapes (count x model with default vCPU/RAM/"
                "100GB storage) at locations that support DEDICATED public IPs. "
                "Billing is per-second against the prepaid balance."
            ),
            options=options,
        )

    def _resolve_option(
        self, *, instance_type: str, region: str, requested_gpu: str | None
    ) -> dict[str, Any]:
        options = to_agent_options(self.client.list_locations(), only_available=False)
        option = find_option(
            options, instance_type=instance_type, region=region or None
        )
        if option is None:
            offered = ", ".join(sorted({o["instance_type"] for o in options})) or "(none)"
            raise BackendValidationError(
                f"TensorDock shape is not offered"
                + (f" in {region}" if region else "")
                + f": {instance_type}. Offered: {offered}."
            )
        if requested_gpu and requested_gpu.upper() not in str(
            option.get("gpu_description") or ""
        ).upper() and requested_gpu.upper() != str(option.get("gpu") or "").upper():
            raise BackendValidationError(
                f"requested gpu {requested_gpu} does not match TensorDock shape "
                f"{instance_type} ({option.get('gpu_description') or 'unknown GPU'})"
            )
        if not option.get("available"):
            raise CapacityUnavailableError(
                f"TensorDock shape {instance_type} has no stock right now. "
                "Call sandbox.options to pick an available shape."
            )
        return option

    def _wait_for_running_instance(self, *, instance_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_status = ""
        while time.monotonic() < deadline:
            instance = self.client.get_instance(instance_id)
            last_status = str(instance.get("status") or "")
            if last_status.lower() in ACTIVE_INSTANCE_STATUSES and _ssh_endpoint(instance)[0]:
                return instance
            if last_status.lower() in TERMINAL_INSTANCE_STATUSES:
                raise BackendUnavailableError(
                    f"TensorDock instance {instance_id} reached terminal status {last_status}"
                )
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(
            f"TensorDock instance {instance_id} did not start before timeout "
            f"(last status: {last_status or 'unknown'})"
        )


def _bootstrap_cloud_init(bootstrap: str) -> dict[str, Any]:
    """Wrap the bash bootstrap in TensorDock's structured cloud_init.

    TensorDock's write_files documents no encoding option, so the script rides
    as plain content (JSON strings carry newlines fine).
    """
    return {
        "write_files": [
            {
                "path": BOOTSTRAP_PATH,
                "content": bootstrap,
                "owner": "root:root",
                "permissions": "0755",
            }
        ],
        "runcmd": [f"bash {BOOTSTRAP_PATH}"],
    }


def _ssh_endpoint(instance: dict[str, Any]) -> tuple[str, int]:
    """Dedicated IP + 22; an explicit forward for internal 22 wins if present."""
    host = str(instance.get("ipAddress") or "")
    for forward in instance.get("portForwards") or []:
        if isinstance(forward, dict) and int(forward.get("internal_port") or 0) == 22:
            external = int(forward.get("external_port") or 0)
            if external:
                return host, external
    return host, 22


def build_tensordock_sandbox_backend(
    *, repo_root: Path | None = None, **_kwargs: Any
) -> TensorDockSandboxBackend:
    # Lazy: the token resolves at call time, not construction.
    return TensorDockSandboxBackend()
