"""Hyperstack (NexGen Cloud) VM sandbox backend.

Provisions a Hyperstack VM inside the configured environment and returns SSH
details to the agent. Hyperstack VMs are secure-by-default with ZERO inbound
ports, so creation attaches an inline ingress rule for TCP 22 — without it
SSH never becomes reachable. Billing is per-minute while the VM exists
(SHUTOFF still bills; only delete stops charges).
"""

from __future__ import annotations

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
from .client import HyperstackClient
from .config import HyperstackSandboxConfig


ACTIVE_VM_STATUSES = frozenset({"ACTIVE"})
# Anything not provably gone stays "alive": SHUTOFF and HIBERNATED still hold
# (and bill for) resources, and a conservative False here is what strands a
# billing VM behind a terminated row.
TERMINAL_VM_STATUSES = frozenset({"DELETED", "DELETING", "ERROR"})

HYPERSTACK_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)

# The one inbound rule a sandbox needs; everything else stays closed.
SSH_INGRESS_RULES: list[dict[str, Any]] = [
    {
        "direction": "ingress",
        "protocol": "tcp",
        "ethertype": "IPv4",
        "remote_ip_prefix": "0.0.0.0/0",
        "port_range_min": 22,
        "port_range_max": 22,
    }
]


class HyperstackSandboxBackend(VmSshSandboxBackend):
    capabilities = BackendCapabilities(
        name="hyperstack",
        lifetime_extension_supported=True,
        requires_hardware_selection=True,
        configurable_resources=False,
    )

    def __init__(
        self,
        *,
        config: HyperstackSandboxConfig | None = None,
        client: HyperstackClient | None = None,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
    ) -> None:
        super().__init__(ssh_runner=ssh_runner, ssh_input_runner=ssh_input_runner)
        # Lazy config/client (mirrors Lambda): the daemon can boot and report
        # health with only an API key; missing settings surface at call time.
        self._config = config
        self._client = client

    @property
    def config(self) -> HyperstackSandboxConfig:
        if self._config is None:
            self._config = HyperstackSandboxConfig.from_env()
        return self._config

    @property
    def client(self) -> HyperstackClient:
        if self._client is None:
            self._client = HyperstackClient(config=self.config.cloud)
        return self._client

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        instance_name = _sandbox_name(request.sandbox_uid or request.experiment_id)
        key_name = f"{instance_name}-key"
        flavor_name = (request.instance_type or self.config.flavor_name or "").strip()
        if not flavor_name:
            raise BackendValidationError(
                "Hyperstack requires an instance_type (a flavor bundling GPU + CPU "
                "+ RAM). Call sandbox.options, or sandbox.request without an "
                "instance_type, to see live availability, then pick one."
            )
        _call(on_phase, "checking_capacity", flavor_name)
        option = self._resolve_flavor(flavor_name=flavor_name, requested_gpu=request.gpu)

        keypair_id = ""
        vm_id = ""
        try:
            _call(on_phase, "registering_ssh_key", key_name)
            keypair = self.client.import_keypair(
                name=key_name,
                environment_name=self.config.environment_name,
                public_key=request.public_key,
            )
            keypair_id = str(keypair.get("id") or "")

            _call(on_phase, "creating", f"{flavor_name} in {self.config.environment_name}")
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
                apt_packages=HYPERSTACK_APT_PACKAGES,
                python_packages=ML_PYTHON_PACKAGES,
            )
            instance = self.client.create_vm(
                name=instance_name,
                environment_name=self.config.environment_name,
                image_name=self.config.image_name,
                flavor_name=flavor_name,
                key_name=key_name,
                user_data=user_data,
                # VMs open ZERO inbound ports by default; without this rule the
                # floating IP exists but SSH never answers.
                security_rules=SSH_INGRESS_RULES,
            )
            vm_id = str(instance.get("id") or "")
            if not vm_id:
                raise BackendUnavailableError("Hyperstack created a VM without an id")
            _call(on_created, vm_id, instance_name)

            _call(on_phase, "connecting", "waiting for active VM and ssh")
            instance = self._wait_for_active_vm(vm_id=vm_id)
            ip = str(instance.get("floating_ip") or "")
            if not ip:
                raise BackendUnavailableError(
                    "Hyperstack VM became active without a floating IP"
                )
            self._wait_for_ssh(host=ip)
            flavor = instance.get("flavor") if isinstance(instance.get("flavor"), dict) else {}
            return ProvisionedSandbox(
                sandbox_id=vm_id,
                ssh_host=ip,
                ssh_port=22,
                ssh_user=self.config.ssh_user,
                workdir=workdir,
                volume_name="",
                sync_dir=workdir,
                unsynced_dir=self.config.sandbox_data_dir,
                sandbox_data_dir=self.config.sandbox_data_dir,
                reused=False,
                gpu=str(option.get("gpu") or flavor.get("gpu") or request.gpu or ""),
                cpu=float(flavor.get("cpu") or option.get("vcpus") or 0) or None,
                memory=(int(flavor.get("ram") or option.get("memory_gib") or 0) * 1024) or None,
                instance_type=flavor_name,
                region=str(
                    (instance.get("environment") or {}).get("region")
                    or (option.get("regions") or [""])[0]
                ),
                price_usd_per_hour=float(option.get("price_usd_per_hour") or 0.0),
            )
        except Exception:
            if vm_id:
                try:
                    self.client.delete_vm(vm_id)
                except Exception:  # noqa: BLE001
                    pass
            if keypair_id:
                try:
                    self.client.delete_keypair(keypair_id)
                except Exception:  # noqa: BLE001
                    pass
            raise

    def is_alive(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            instance = self.client.get_vm(sandbox_id)
        except BackendUnavailableError as exc:
            if exc.status == 404:
                return False  # authoritative: the VM no longer exists
            raise  # outage/timeout — callers must not read this as "gone"
        return str(instance.get("status") or "") not in TERMINAL_VM_STATUSES

    def terminate(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        keypair_names = self._keypair_names_for_vm(sandbox_id=sandbox_id)
        try:
            self.client.delete_vm(sandbox_id)
        except BackendUnavailableError as exc:
            if exc.status != 404:  # 404 = already gone; that IS terminated
                return False
        except Exception:  # noqa: BLE001
            return False
        self._delete_keypairs_by_name(keypair_names)
        return True

    def health(self) -> dict:
        try:
            self.client.list_flavors()
            return {"ok": True, "backend": "hyperstack"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": "hyperstack", "error": str(exc)}

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        name = _sandbox_name(sandbox_uid or experiment_id)
        try:
            for instance in self.client.list_vms():
                if (
                    instance.get("name") == name
                    and str(instance.get("status") or "") not in TERMINAL_VM_STATUSES
                ):
                    return str(instance.get("id") or "") or None
        except Exception:  # noqa: BLE001
            return None
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Live menu of in-stock Hyperstack flavors, priced from the pricebook."""
        options = to_agent_options(
            self.client.list_flavors(region=region),
            self.client.get_pricebook(),
            gpu=gpu,
            region=region,
            only_available=True,
        )
        regions = sorted({r for option in options for r in option.get("regions", [])})
        return {
            "provider": "hyperstack",
            "selection_required": True,
            "select_with": "instance_type",
            "reason": (
                "Hyperstack bundles GPU, CPU, and RAM into fixed flavors; pick one "
                "instance_type. Billing is per-minute while the VM exists."
            ),
            "regions": regions,
            "count": len(options),
            "options": options,
        }

    def _resolve_flavor(
        self, *, flavor_name: str, requested_gpu: str | None
    ) -> dict[str, Any]:
        """Validate the flavor exists with stock right now; return its option."""
        options = to_agent_options(
            self.client.list_flavors(), self.client.get_pricebook(), only_available=False
        )
        option = find_option(options, instance_type=flavor_name)
        if option is None:
            offered = ", ".join(sorted(o["instance_type"] for o in options)) or "(none)"
            raise BackendValidationError(
                f"Hyperstack flavor is not offered: {flavor_name}. Offered: {offered}."
            )
        if requested_gpu and requested_gpu.upper() not in str(
            option.get("gpu_description") or ""
        ).upper():
            raise BackendValidationError(
                f"requested gpu {requested_gpu} does not match Hyperstack flavor "
                f"{flavor_name} ({option.get('gpu_description') or 'no GPU'})"
            )
        if not option.get("available"):
            raise CapacityUnavailableError(
                f"Hyperstack flavor {flavor_name} has no stock right now. "
                "Call sandbox.options to pick an available flavor."
            )
        return option

    def _wait_for_active_vm(self, *, vm_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_status = ""
        while time.monotonic() < deadline:
            instance = self.client.get_vm(vm_id)
            last_status = str(instance.get("status") or "")
            if last_status in ACTIVE_VM_STATUSES and instance.get("floating_ip"):
                return instance
            if last_status in TERMINAL_VM_STATUSES:
                raise BackendUnavailableError(
                    f"Hyperstack VM {vm_id} reached terminal status {last_status}"
                )
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(
            f"Hyperstack VM {vm_id} did not become active before timeout "
            f"(last status: {last_status or 'unknown'})"
        )

    def _keypair_names_for_vm(self, *, sandbox_id: str) -> list[str]:
        try:
            instance = self.client.get_vm(sandbox_id)
        except Exception:  # noqa: BLE001
            return []
        keypair = instance.get("keypair")
        name = str(keypair.get("name") or "") if isinstance(keypair, dict) else ""
        return [name] if name.startswith("rp-") else []

    def _delete_keypairs_by_name(self, names: list[str]) -> None:
        if not names:
            return
        wanted = set(names)
        try:
            keypairs = self.client.list_keypairs()
        except Exception:  # noqa: BLE001
            return
        for keypair in keypairs:
            if str(keypair.get("name") or "") in wanted and keypair.get("id"):
                try:
                    self.client.delete_keypair(keypair["id"])
                except Exception:  # noqa: BLE001
                    pass


def _sandbox_name(experiment_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", experiment_id.lower()).strip("-")
    return f"rp-{safe or 'exp'}"[:50]  # Hyperstack caps VM names at 50 chars


def _call(cb: Any, *args: Any) -> None:
    if cb is not None:
        cb(*args)


def build_hyperstack_sandbox_backend(
    *, repo_root: Path | None = None, **_kwargs: Any
) -> HyperstackSandboxBackend:
    # Lazy: credentials/environment resolve at call time, not construction.
    return HyperstackSandboxBackend()
