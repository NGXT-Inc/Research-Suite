"""Lambda Labs VM sandbox backend.

This backend provisions a Lambda Cloud VM and returns SSH details to the agent.
Output retention is explicit over SSH; this backend only prepares a normal
developer shell with the tools agents expect.
"""

from __future__ import annotations

from contextlib import suppress
import time
from pathlib import Path
from typing import Any, Mapping

from .._values import _int_or_zero
from ...bootstrap_tools import (
    BASELINE_APT_PACKAGES,
    ML_PYTHON_PACKAGES,
)
from ...vm_bootstrap import build_standard_user_data
from ....sandbox_backend import (
    BackendUnavailableError,
    BackendCapabilities,
    BackendValidationError,
    CapacityUnavailableError,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxRequest,
)
from ....sandbox_paths import remote_experiment_dir, remote_root_of, remote_sessions_dir
from ..vm_ssh_backend import SshInputRunner, SshRunner, VmSshSandboxBackend, _vm_name as _sandbox_name
from .catalog import find_option, summarize_instance_types, to_agent_options
from .client import LambdaCloudClient
from .config import LambdaSandboxConfig


ACTIVE_INSTANCE_STATUSES = frozenset({"active"})
LIVE_INSTANCE_STATUSES = frozenset({"booting", "active", "unhealthy"})


LAMBDA_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)

class LambdaLabsSandboxBackend(VmSshSandboxBackend):
    # Lambda Labs sells fixed machine SKUs (GPU + vCPU + RAM bundled), so the
    # agent must pick an instance type — there are no independent cpu/memory
    # knobs. ``requires_hardware_selection`` makes SandboxService return a live
    # availability menu when ``sandbox.request`` arrives without an instance type.
    capabilities = BackendCapabilities(
        name="lambda_labs",
        lifetime_extension_supported=True,
        requires_hardware_selection=True,
        configurable_resources=False,
    )

    def __init__(
        self,
        *,
        config: LambdaSandboxConfig | None = None,
        client: LambdaCloudClient | None = None,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
    ) -> None:
        super().__init__(
            ssh_runner=ssh_runner,
            ssh_input_runner=ssh_input_runner,
        )
        # Resolve config/client lazily so the daemon can boot (and report health)
        # with only an API key present — region/instance type are per-request,
        # and a missing key surfaces at call time as a clean health error rather
        # than crashing construction of the default backend.
        self._config = config
        self._client = client

    @property
    def config(self) -> LambdaSandboxConfig:
        if self._config is None:
            self._config = LambdaSandboxConfig.from_env()
        return self._config

    @property
    def client(self) -> LambdaCloudClient:
        if self._client is None:
            self._client = LambdaCloudClient(config=self.config.cloud)
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
        instance_type = (request.instance_type or self.config.instance_type_name or "").strip()
        if not instance_type:
            raise BackendValidationError(
                "Lambda Labs requires an instance_type (it bundles GPU + CPU + RAM "
                "into one machine). Call sandbox.options, or sandbox.request without "
                "an instance_type, to see live availability, then pick a SKU."
            )
        # The config's default region pairs with the config's default instance
        # type. If the agent overrode the instance type, don't force that region
        # onto it — auto-pick a region with capacity for the chosen SKU instead.
        default_region = "" if request.instance_type else self.config.region_name
        self._notify(on_phase, "checking_capacity", instance_type)
        region, specs = self._resolve_placement(
            instance_type=instance_type,
            region=(request.region or default_region or "").strip(),
            requested_gpu=request.gpu,
        )

        self._notify(on_phase, "registering_ssh_key", key_name)
        key_id = ""
        instance_id = ""
        try:
            key = self.client.add_ssh_key(name=key_name, public_key=request.public_key)
            key_id = str(key.get("id") or "")

            self._notify(on_phase, "creating", f"{instance_type} in {region}")
            workdir = request.remote_workdir or remote_experiment_dir(
                experiment_id=request.experiment_id, root=self.config.remote_root
            )
            user_data = build_user_data(
                public_key=request.public_key,
                experiment_id=request.experiment_id,
                workdir=workdir,
                sessions_dir=remote_sessions_dir(
                    experiment_id=request.experiment_id, root=remote_root_of(workdir)
                ),
                sandbox_data_dir=self.config.sandbox_data_dir,
                management_public_key=request.management_public_key,
                # No tokens embedded in user_data (plan Phase 9, risk 16): they
                # are delivered post-boot via write_secrets over the mgmt channel.
            )
            instance_id = self.client.launch_instance(
                region_name=region,
                instance_type_name=instance_type,
                ssh_key_name=key_name,
                name=instance_name,
                user_data=user_data,
            )
            self._notify(on_created, instance_id, instance_name)

            self._notify(on_phase, "connecting", "waiting for active instance and ssh")
            instance = self._wait_for_active_instance(instance_id=instance_id)
            ip = str(instance.get("ip") or instance.get("hostname") or "")
            if not ip:
                raise BackendUnavailableError("Lambda instance became active without a public IP")
            self._wait_for_ssh(host=ip)
            return ProvisionedSandbox(
                sandbox_id=instance_id,
                ssh_host=ip,
                ssh_port=22,
                ssh_user=self.config.ssh_user,
                workdir=workdir,
                volume_name="",
                sync_dir=workdir,
                unsynced_dir=self.config.sandbox_data_dir,
                sandbox_data_dir=self.config.sandbox_data_dir,
                reused=False,
                gpu=str(specs.get("gpu") or request.gpu or ""),
                cpu=float(specs["vcpus"]) if specs.get("vcpus") else None,
                memory=int(specs["memory_gib"]) * 1024 if specs.get("memory_gib") else None,
                instance_type=instance_type,
                region=region,
                price_usd_per_hour=float(specs.get("price_usd_per_hour") or 0.0),
            )
        except Exception:
            if instance_id:
                with suppress(Exception):
                    self.client.terminate_instances([instance_id])
            if key_id:
                with suppress(Exception):
                    self.client.delete_ssh_key(key_id)
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
        return str(instance.get("status") or "") in LIVE_INSTANCE_STATUSES

    def terminate(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        key_names = self._ssh_key_names_for_instance(sandbox_id=sandbox_id)
        try:
            self.client.terminate_instances([sandbox_id])
        except Exception:  # noqa: BLE001
            return False
        self._delete_ssh_keys_by_name(key_names)
        return True

    def health(self) -> dict:
        return self._probe_health(lambda: self.client.list_instance_types())

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        name = _sandbox_name(sandbox_uid or experiment_id)
        try:
            for instance in self.client.list_instances():
                if instance.get("name") == name and str(instance.get("status") or "") in LIVE_INSTANCE_STATUSES:
                    return str(instance.get("id") or "") or None
        except Exception:  # noqa: BLE001
            return None
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Live, agent-facing menu of currently-available Lambda machine SKUs.

        Returns a compact, cheapest-first list of instance types with capacity
        right now. The agent picks one and passes it back as
        ``sandbox.request(instance_type=...)``.
        """
        summary = summarize_instance_types(
            self.client.list_instance_types(),
            gpu=gpu,
            region=region,
            only_available=True,
        )
        options = to_agent_options(summary)
        return self._selection_catalog(
            reason=(
                "Lambda Labs bundles GPU, CPU, and RAM into fixed machine types; "
                "pick one instance_type rather than cpu/memory."
            ),
            regions=summary["regions"],
            options=options,
        )

    def _resolve_placement(
        self, *, instance_type: str, region: str, requested_gpu: str | None
    ) -> tuple[str, dict[str, Any]]:
        """Validate the SKU + capacity and pick a region; return (region, specs).

        Region resolution: honor an explicit request, otherwise pick the
        (sorted, deterministic) first region that currently has capacity for the
        chosen instance type.
        """
        instance_types = self.client.list_instance_types()
        row = instance_types.get(instance_type)
        if not isinstance(row, dict):
            offered = ", ".join(sorted(instance_types)) or "(none)"
            raise BackendValidationError(
                f"Lambda instance type is not currently offered: {instance_type}. "
                f"Currently offered: {offered}."
            )
        instance = row.get("instance_type")
        if not isinstance(instance, dict):
            raise BackendUnavailableError("Lambda Cloud returned malformed instance type data")
        if requested_gpu:
            gpu_text = " ".join(
                str(instance.get(key) or "")
                for key in ("name", "description", "gpu_description")
            ).upper()
            if requested_gpu.upper() not in gpu_text:
                raise BackendValidationError(
                    f"requested gpu {requested_gpu} does not match Lambda instance "
                    f"type {instance_type} ({instance.get('gpu_description') or 'unknown GPU'})"
                )
        regions = row.get("regions_with_capacity_available")
        if not isinstance(regions, list):
            raise BackendUnavailableError("Lambda Cloud returned malformed capacity data")
        available_regions = sorted(
            str(item.get("name") or "")
            for item in regions
            if isinstance(item, dict) and item.get("name")
        )
        if region:
            if region not in available_regions:
                where = ", ".join(available_regions) or "(no regions)"
                raise CapacityUnavailableError(
                    f"Lambda instance type {instance_type} has no current capacity in "
                    f"{region}. Regions with capacity now: {where}."
                )
            chosen = region
        else:
            if not available_regions:
                raise CapacityUnavailableError(
                    f"Lambda instance type {instance_type} has no current capacity in "
                    "any region. Call sandbox.options to pick an available SKU."
                )
            chosen = available_regions[0]
        specs_raw = instance.get("specs") if isinstance(instance.get("specs"), dict) else {}
        option = find_option(
            summarize_instance_types(instance_types, only_available=False),
            instance_type=instance_type,
        ) or {}
        specs = {
            "gpu": option.get("gpu") or str(instance.get("gpu_description") or ""),
            "gpus": _int_or_zero(specs_raw.get("gpus")),
            "vcpus": _int_or_zero(specs_raw.get("vcpus")),
            "memory_gib": _int_or_zero(specs_raw.get("memory_gib")),
            # Cloud plan Phase 7: the price the catalog quoted for this SKU,
            # captured here instead of being fetched-then-discarded.
            "price_usd_per_hour": float(option.get("price_usd_per_hour") or 0.0),
        }
        return chosen, specs

    def _wait_for_active_instance(self, *, instance_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_status = ""
        while time.monotonic() < deadline:
            instance = self.client.get_instance(instance_id)
            last_status = str(instance.get("status") or "")
            if last_status in ACTIVE_INSTANCE_STATUSES and (instance.get("ip") or instance.get("hostname")):
                return instance
            if last_status in {"terminated", "terminating", "preempted"}:
                raise BackendUnavailableError(
                    f"Lambda instance {instance_id} reached terminal status {last_status}"
                )
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(
            f"Lambda instance {instance_id} did not become active before timeout "
            f"(last status: {last_status or 'unknown'})"
        )

    def _ssh_key_names_for_instance(self, *, sandbox_id: str) -> list[str]:
        try:
            instance = self.client.get_instance(sandbox_id)
        except Exception:  # noqa: BLE001
            return []
        names = instance.get("ssh_key_names")
        if not isinstance(names, list):
            return []
        return [str(name) for name in names if str(name).startswith("rp-")]

    def _delete_ssh_keys_by_name(self, names: list[str]) -> None:
        if not names:
            return
        wanted = set(names)
        try:
            keys = self.client.list_ssh_keys()
        except Exception:  # noqa: BLE001
            return
        for key in keys:
            key_name = str(key.get("name") or "")
            key_id = str(key.get("id") or "")
            if key_name in wanted and key_id:
                with suppress(Exception):
                    self.client.delete_ssh_key(key_id)


def build_user_data(
    *,
    public_key: str,
    experiment_id: str,
    workdir: str,
    sessions_dir: str,
    sandbox_data_dir: str,
    management_public_key: str = "",
    tokens: Mapping[str, str] | None = None,
) -> str:
    _ = tokens  # Compatibility input; credentials are delivered post-boot.
    return build_standard_user_data(
        public_key=public_key,
        experiment_id=experiment_id,
        workdir=workdir,
        sessions_dir=sessions_dir,
        sandbox_data_dir=sandbox_data_dir,
        management_public_key=management_public_key,
        apt_packages=LAMBDA_APT_PACKAGES,
        python_packages=ML_PYTHON_PACKAGES,
    )


def build_lambda_labs_sandbox_backend(*, repo_root: Path | None = None, **_kwargs: Any) -> LambdaLabsSandboxBackend:
    # Lazy: do not resolve credentials/region/instance type at construction so
    # the default backend can be built (and health-checked) with only an API key.
    return LambdaLabsSandboxBackend()
