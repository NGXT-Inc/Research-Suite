"""Verda (formerly DataCrunch) VM sandbox backend.

Provisions a Verda instance and returns SSH details to the agent. SSH keys
and the bootstrap startup script are pre-registered account resources (both
rp-named), referenced by id at deploy and deleted again at terminate.
Billing rounds UP to 10-minute increments.
"""

from __future__ import annotations

import os
import re
import socket
import time
from pathlib import Path
from typing import Any

from backend.execution.bootstrap_tools import BASELINE_APT_PACKAGES, ML_PYTHON_PACKAGES
from backend.execution.vm_bootstrap import build_standard_user_data
from ....sandbox.sandbox_backend import (
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
from .client import VerdaClient
from .config import VerdaSandboxConfig


ACTIVE_INSTANCE_STATUSES = frozenset({"running"})
# "offline" instances still hold (and bill for) their OS volume; only these
# statuses are provably done. "no_capacity"/"installation_failed"/"error" are
# deploy outcomes handled in the wait loop.
TERMINAL_INSTANCE_STATUSES = frozenset({"discontinued", "deleting", "notfound"})
FAILED_DEPLOY_STATUSES = frozenset({"error", "installation_failed"})

VERDA_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)


class VerdaSandboxBackend(VmSshSandboxBackend):
    capabilities = BackendCapabilities(
        name="verda",
        lifetime_extension_supported=True,
        requires_hardware_selection=True,
        configurable_resources=False,
    )

    def __init__(
        self,
        *,
        config: VerdaSandboxConfig | None = None,
        client: VerdaClient | None = None,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
    ) -> None:
        super().__init__(ssh_runner=ssh_runner, ssh_input_runner=ssh_input_runner)
        self._config = config
        self._client = client

    @property
    def config(self) -> VerdaSandboxConfig:
        if self._config is None:
            self._config = VerdaSandboxConfig.from_env()
        return self._config

    @property
    def client(self) -> VerdaClient:
        if self._client is None:
            self._client = VerdaClient(config=self.config.cloud)
        return self._client

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        instance_name = _sandbox_name(request.sandbox_uid or request.experiment_id)
        instance_type = (request.instance_type or self.config.instance_type or "").strip()
        if not instance_type:
            raise BackendValidationError(
                "Verda requires an instance_type (a fixed GPU + CPU + RAM SKU, "
                "e.g. 1H100.80S.30V). Call sandbox.options, or sandbox.request "
                "without an instance_type, to see live availability, then pick one."
            )
        _call(on_phase, "checking_capacity", instance_type)
        option, location = self._resolve_placement(
            instance_type=instance_type,
            location=(request.region or self.config.location_code or "").strip(),
            requested_gpu=request.gpu,
        )

        key_id = ""
        script_id = ""
        instance_id = ""
        try:
            _call(on_phase, "registering_ssh_key", f"{instance_name}-key")
            key_id = self.client.add_ssh_key(
                name=f"{instance_name}-key", key=request.public_key
            )

            workdir = request.remote_workdir or remote_experiment_dir(
                experiment_id=request.experiment_id, root=self.config.remote_root
            )
            script = build_standard_user_data(
                public_key=request.public_key,
                experiment_id=request.experiment_id,
                workdir=workdir,
                sessions_dir=remote_sessions_dir(
                    experiment_id=request.experiment_id, root=remote_root_of(workdir)
                ),
                sandbox_data_dir=self.config.sandbox_data_dir,
                management_public_key=request.management_public_key,
                apt_packages=VERDA_APT_PACKAGES,
                python_packages=ML_PYTHON_PACKAGES,
            )
            script_id = self.client.add_script(
                name=f"{instance_name}-boot", script=script
            )

            _call(on_phase, "creating", f"{instance_type} in {location}")
            instance_id = self.client.deploy_instance(
                instance_type=instance_type,
                image=self.config.image,
                hostname=instance_name,
                description=instance_name,
                location_code=location,
                ssh_key_ids=[key_id],
                startup_script_id=script_id,
            )
            _call(on_created, instance_id, instance_name)

            _call(on_phase, "connecting", "waiting for running instance and ssh")
            instance = self._wait_for_running_instance(instance_id=instance_id)
            ip = str(instance.get("ip") or "")
            if not ip:
                raise BackendUnavailableError("Verda instance is running without an IP")
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
                gpu=str(option.get("gpu") or request.gpu or ""),
                cpu=float(option.get("vcpus") or 0) or None,
                memory=(int(option.get("memory_gib") or 0) * 1024) or None,
                instance_type=instance_type,
                region=location,
                # Prefer the live per-instance quote (spot/dynamic pricing).
                price_usd_per_hour=_float_or_zero(instance.get("price_per_hour"))
                or float(option.get("price_usd_per_hour") or 0.0),
            )
        except Exception:
            if instance_id:
                try:
                    self.client.perform_action(instance_id=instance_id, action="delete")
                except Exception:  # noqa: BLE001
                    pass
            for cleanup, resource_id in (
                (self.client.delete_script, script_id),
                (self.client.delete_ssh_key, key_id),
            ):
                if resource_id:
                    try:
                        cleanup(resource_id)
                    except Exception:  # noqa: BLE001
                        pass
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
        return str(instance.get("status") or "") not in TERMINAL_INSTANCE_STATUSES

    def terminate(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            self.client.perform_action(instance_id=sandbox_id, action="delete")
        except BackendUnavailableError as exc:
            if exc.status != 404:  # 404 = already gone; that IS terminated
                return False
        except Exception:  # noqa: BLE001
            return False
        self._delete_rp_resources(sandbox_id=sandbox_id)
        return True

    def sandbox_environment(self) -> dict:
        available_tokens: list[str] = []
        if os.environ.get("HF_TOKEN"):
            available_tokens.append("HF_TOKEN")
        return {
            "available_tokens": available_tokens,
            "notes": (
                [
                    "HF_TOKEN is available inside the sandbox for Hugging Face downloads. "
                    "Do not print or write the token; use it through Hugging Face tooling."
                ]
                if available_tokens
                else []
            ),
        }

    def health(self) -> dict:
        try:
            self.client.list_instance_types()
            return {"ok": True, "backend": "verda"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": "verda", "error": str(exc)}

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        name = _sandbox_name(sandbox_uid or experiment_id)
        try:
            for instance in self.client.list_instances():
                if (
                    instance.get("hostname") == name
                    and str(instance.get("status") or "") not in TERMINAL_INSTANCE_STATUSES
                ):
                    return str(instance.get("id") or "") or None
        except Exception:  # noqa: BLE001
            return None
        return None

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Live menu of deployable Verda SKUs with current pricing."""
        options = to_agent_options(
            self.client.list_instance_types(),
            self.client.list_availability(),
            gpu=gpu,
            region=region,
            only_available=True,
        )
        regions = sorted({r for option in options for r in option.get("regions", [])})
        return {
            "provider": "verda",
            "selection_required": True,
            "select_with": "instance_type",
            "reason": (
                "Verda (DataCrunch) bundles GPU, CPU, and RAM into fixed instance "
                "types; pick one instance_type. Billing rounds up to 10-minute "
                "increments."
            ),
            "regions": regions,
            "count": len(options),
            "options": options,
        }

    def _resolve_placement(
        self, *, instance_type: str, location: str, requested_gpu: str | None
    ) -> tuple[dict[str, Any], str]:
        """Validate the SKU and pick a location with capacity for it now."""
        options = to_agent_options(
            self.client.list_instance_types(),
            self.client.list_availability(),
            only_available=False,
        )
        option = find_option(options, instance_type=instance_type)
        if option is None:
            offered = ", ".join(sorted(o["instance_type"] for o in options)) or "(none)"
            raise BackendValidationError(
                f"Verda instance type is not offered: {instance_type}. "
                f"Offered: {offered}."
            )
        if requested_gpu and requested_gpu.upper() not in str(
            option.get("gpu_description") or ""
        ).upper() and requested_gpu.upper() not in str(option.get("gpu") or "").upper():
            raise BackendValidationError(
                f"requested gpu {requested_gpu} does not match Verda instance type "
                f"{instance_type} ({option.get('gpu_description') or 'unknown GPU'})"
            )
        available = sorted(str(r) for r in option.get("regions", []))
        if location:
            if location not in available:
                where = ", ".join(available) or "(no locations)"
                raise CapacityUnavailableError(
                    f"Verda instance type {instance_type} has no capacity in "
                    f"{location}. Locations with capacity now: {where}."
                )
            chosen = location
        else:
            if not available:
                raise CapacityUnavailableError(
                    f"Verda instance type {instance_type} has no capacity in any "
                    "location. Call sandbox.options to pick an available SKU."
                )
            chosen = available[0]
        return option, chosen

    def _wait_for_running_instance(self, *, instance_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_status = ""
        while time.monotonic() < deadline:
            instance = self.client.get_instance(instance_id)
            last_status = str(instance.get("status") or "")
            if last_status in ACTIVE_INSTANCE_STATUSES and instance.get("ip"):
                return instance
            if last_status == "no_capacity":
                raise CapacityUnavailableError(
                    f"Verda ran out of capacity while deploying {instance_id}"
                )
            if last_status in FAILED_DEPLOY_STATUSES | TERMINAL_INSTANCE_STATUSES:
                raise BackendUnavailableError(
                    f"Verda instance {instance_id} reached terminal status {last_status}"
                )
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(
            f"Verda instance {instance_id} did not start before timeout "
            f"(last status: {last_status or 'unknown'})"
        )

    def _wait_for_ssh(self, *, host: str) -> None:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, 22), timeout=10):
                    return
            except OSError as exc:
                last_error = str(exc)
                time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(f"SSH never became reachable on {host}:22 ({last_error})")

    def _delete_rp_resources(self, *, sandbox_id: str) -> None:
        """Drop the rp-named key + script registered for this instance.

        Resolved by name (rp-<uid>-key / rp-<uid>-boot) because the ids are
        only known to the acquire that created them, and terminate may run
        after a daemon restart.
        """
        try:
            instance = self.client.get_instance(sandbox_id)
            hostname = str(instance.get("hostname") or "")
        except Exception:  # noqa: BLE001
            hostname = ""
        if not hostname.startswith("rp-"):
            return
        for lister, deleter, suffix in (
            (self.client.list_ssh_keys, self.client.delete_ssh_key, "-key"),
            (self.client.list_scripts, self.client.delete_script, "-boot"),
        ):
            try:
                resources = lister()
            except Exception:  # noqa: BLE001
                continue
            for resource in resources:
                if str(resource.get("name") or "") == f"{hostname}{suffix}" and resource.get("id"):
                    try:
                        deleter(str(resource["id"]))
                    except Exception:  # noqa: BLE001
                        pass


def _sandbox_name(experiment_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", experiment_id.lower()).strip("-")
    return f"rp-{safe or 'exp'}"[:60]


def _call(cb: Any, *args: Any) -> None:
    if cb is not None:
        cb(*args)


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_verda_sandbox_backend(
    *, repo_root: Path | None = None, **_kwargs: Any
) -> VerdaSandboxBackend:
    # Lazy: OAuth2 credentials resolve at call time, not construction.
    return VerdaSandboxBackend()
