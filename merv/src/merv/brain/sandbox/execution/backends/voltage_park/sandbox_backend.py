"""Voltage Park instant-VM sandbox backend.

Provisions an on-demand H100 VM from an instant-deploy preset and returns
SSH details to the agent. The bootstrap script rides in as a structured
cloud-init (write_files + runcmd) because the instant API takes a cloud-init
object, not a raw user_data blob; SSH public keys are passed per-deploy.

NEEDS LIVE SMOKE TEST: whether the returned public_ip serves port 22
directly. The implementation assumes it does, and falls back to a port
forward mapping internal port 22 when the VM detail reports one.
"""

from __future__ import annotations

import base64
import re
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
from ...sync_dirs import remote_experiment_dir, remote_root_of, remote_sessions_dir
from ..vm_ssh_backend import SshInputRunner, SshRunner, VmSshSandboxBackend
from .catalog import find_option, to_agent_options
from .client import VoltageParkClient
from .config import VoltageParkSandboxConfig


ACTIVE_VM_STATUSES = frozenset({"Running"})
# Stopped/StoppedDisassociated still hold storage; Outbid is spot-only and
# should not occur for instant (on-demand) VMs but is not provably gone.
TERMINAL_VM_STATUSES = frozenset({"Terminated"})

VOLTAGE_PARK_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)

BOOTSTRAP_PATH = "/opt/merv/bootstrap.sh"


class VoltageParkSandboxBackend(VmSshSandboxBackend):
    capabilities = BackendCapabilities(
        name="voltage_park",
        lifetime_extension_supported=True,
        requires_hardware_selection=True,
        configurable_resources=False,
    )

    def __init__(
        self,
        *,
        config: VoltageParkSandboxConfig | None = None,
        client: VoltageParkClient | None = None,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
    ) -> None:
        super().__init__(ssh_runner=ssh_runner, ssh_input_runner=ssh_input_runner)
        self._config = config
        self._client = client

    @property
    def config(self) -> VoltageParkSandboxConfig:
        if self._config is None:
            self._config = VoltageParkSandboxConfig.from_env()
        return self._config

    @property
    def client(self) -> VoltageParkClient:
        if self._client is None:
            self._client = VoltageParkClient(config=self.config.cloud)
        return self._client

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        vm_name = _sandbox_name(request.sandbox_uid or request.experiment_id)
        config_id = (request.instance_type or "").strip()
        if not config_id:
            raise BackendValidationError(
                "Voltage Park requires an instance_type (an instant-deploy preset "
                "id). Call sandbox.options, or sandbox.request without an "
                "instance_type, to see live presets, then pick one."
            )
        _call(on_phase, "checking_capacity", config_id)
        option = self._resolve_preset(
            config_id=config_id,
            region=(request.region or "").strip(),
            requested_gpu=request.gpu,
        )

        vm_id = ""
        try:
            _call(on_phase, "creating", f"preset {config_id}")
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
                apt_packages=VOLTAGE_PARK_APT_PACKAGES,
                python_packages=ML_PYTHON_PACKAGES,
            )
            vm_id = self.client.create_instant_vm(
                config_id=config_id,
                name=vm_name,
                # Per-deploy raw public keys; the bootstrap re-authorizes both
                # for root + the management principal.
                ssh_keys=[
                    key
                    for key in (request.public_key, request.management_public_key)
                    if key
                ],
                cloud_init=_bootstrap_cloud_init(bootstrap),
            )
            _call(on_created, vm_id, vm_name)

            _call(on_phase, "connecting", "waiting for running VM and ssh")
            vm = self._wait_for_running_vm(vm_id=vm_id)
            host, port = _ssh_endpoint(vm)
            if not host:
                raise BackendUnavailableError(
                    "Voltage Park VM is running without a public IP"
                )
            self._wait_for_ssh(host=host, port=port)
            return ProvisionedSandbox(
                sandbox_id=vm_id,
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
                cpu=float(option.get("vcpus") or 0) or None,
                memory=(int(option.get("memory_gib") or 0) * 1024) or None,
                instance_type=config_id,
                region=str((option.get("regions") or [""])[0]),
                price_usd_per_hour=_vm_hourly_rate(vm)
                or float(option.get("price_usd_per_hour") or 0.0),
            )
        except Exception:
            if vm_id:
                try:
                    self.client.delete_vm(vm_id)
                except Exception:  # noqa: BLE001
                    pass
            raise

    def is_alive(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            vm = self.client.get_vm(sandbox_id)
        except BackendUnavailableError as exc:
            if exc.status == 404:
                return False  # authoritative: the VM no longer exists
            raise  # outage/timeout — callers must not read this as "gone"
        return str(vm.get("status") or "") not in TERMINAL_VM_STATUSES

    def terminate(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            self.client.delete_vm(sandbox_id)
        except BackendUnavailableError as exc:
            if exc.status != 404:  # 404 = already gone; that IS terminated
                return False
        except Exception:  # noqa: BLE001
            return False
        return True

    def health(self) -> dict:
        try:
            self.client.list_instant_locations()
            return {"ok": True, "backend": "voltage_park"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": "voltage_park", "error": str(exc)}

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        name = _sandbox_name(sandbox_uid or experiment_id)
        try:
            for vm in self.client.list_vms():
                if (
                    vm.get("name") == name
                    and str(vm.get("status") or "") not in TERMINAL_VM_STATUSES
                ):
                    return str(vm.get("id") or "") or None
        except Exception:  # noqa: BLE001
            return None
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Live menu of instant-deploy presets (H100-only on-demand fleet)."""
        options = to_agent_options(
            self.client.list_instant_locations(),
            gpu=gpu,
            region=region,
            only_available=True,
        )
        regions = sorted({r for option in options for r in option.get("regions", [])})
        return {
            "provider": "voltage_park",
            "selection_required": True,
            "select_with": "instance_type",
            "reason": (
                "Voltage Park sells fixed instant-deploy presets (H100 SXM5 "
                "machines in 1/2/4/8-GPU shapes); pick one options[].instance_type "
                "(a preset id)."
            ),
            "regions": regions,
            "count": len(options),
            "options": options,
        }

    def _resolve_preset(
        self, *, config_id: str, region: str, requested_gpu: str | None
    ) -> dict[str, Any]:
        options = to_agent_options(
            self.client.list_instant_locations(), only_available=False
        )
        option = find_option(options, instance_type=config_id)
        if option is None:
            offered = ", ".join(sorted(o["instance_type"] for o in options)) or "(none)"
            raise BackendValidationError(
                f"Voltage Park preset is not offered: {config_id}. Offered: {offered}."
            )
        if requested_gpu and requested_gpu.upper() not in str(
            option.get("gpu_description") or ""
        ).upper() and requested_gpu.upper() != str(option.get("gpu") or "").upper():
            raise BackendValidationError(
                f"requested gpu {requested_gpu} does not match Voltage Park preset "
                f"{config_id} ({option.get('gpu_description') or 'unknown GPU'})"
            )
        if region and region not in (option.get("regions") or []):
            where = ", ".join(option.get("regions") or []) or "(no locations)"
            raise CapacityUnavailableError(
                f"Voltage Park preset {config_id} has no capacity in {region}. "
                f"Locations with capacity now: {where}."
            )
        if not option.get("available"):
            raise CapacityUnavailableError(
                f"Voltage Park preset {config_id} has no available VMs right now. "
                "Call sandbox.options to pick an available preset."
            )
        return option

    def _wait_for_running_vm(self, *, vm_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_status = ""
        while time.monotonic() < deadline:
            vm = self.client.get_vm(vm_id)
            last_status = str(vm.get("status") or "")
            if last_status in ACTIVE_VM_STATUSES and vm.get("public_ip"):
                return vm
            if last_status in TERMINAL_VM_STATUSES:
                raise BackendUnavailableError(
                    f"Voltage Park VM {vm_id} reached terminal status {last_status}"
                )
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(
            f"Voltage Park VM {vm_id} did not start before timeout "
            f"(last status: {last_status or 'unknown'})"
        )


def _bootstrap_cloud_init(bootstrap: str) -> dict[str, Any]:
    """Wrap the bash bootstrap in the instant API's structured cloud-init."""
    return {
        "write_files": [
            {
                "path": BOOTSTRAP_PATH,
                "content": base64.b64encode(bootstrap.encode("utf-8")).decode("ascii"),
                "encoding": "b64",
                "permissions": "0755",
                "owner": "root:root",
            }
        ],
        "runcmd": [f"bash {BOOTSTRAP_PATH}"],
    }


def _ssh_endpoint(vm: dict[str, Any]) -> tuple[str, int]:
    """Public IP + SSH port; a port forward for internal 22 wins when present."""
    host = str(vm.get("public_ip") or "")
    for forward in vm.get("port_forwards") or []:
        if isinstance(forward, dict) and int(forward.get("internal_port") or 0) == 22:
            external = int(forward.get("external_port") or 0)
            if external:
                return host, external
    return host, 22


def _vm_hourly_rate(vm: dict[str, Any]) -> float:
    pricing = vm.get("pricing")
    if not isinstance(pricing, dict):
        return 0.0
    try:
        return float(pricing.get("total_associated_per_hr") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _sandbox_name(experiment_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", experiment_id.lower()).strip("-")
    return f"rp-{safe or 'exp'}"[:60]


def _call(cb: Any, *args: Any) -> None:
    if cb is not None:
        cb(*args)


def build_voltage_park_sandbox_backend(
    *, repo_root: Path | None = None, **_kwargs: Any
) -> VoltageParkSandboxBackend:
    # Lazy: the token resolves at call time, not construction.
    return VoltageParkSandboxBackend()
