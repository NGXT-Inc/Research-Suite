"""Thunder Compute VM sandbox backend."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.execution.bootstrap_tools import BASELINE_APT_PACKAGES, ML_PYTHON_PACKAGES
from backend.execution.vm_bootstrap import (
    DASHBOARD_PORTS,
    MGMT_SSH_USER,
    build_bootstrap_core,
)
from backend.execution.vm_ssh import (
    ssh_command,
    stderr_detail,
)
from ....sandbox.sandbox_backend import (
    BackendUnavailableError,
    BackendCapabilities,
    BackendValidationError,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxRequest,
)
from ...sync_dirs import remote_experiment_dir, remote_root_of, remote_sessions_dir
from ..vm_ssh_backend import SshInputRunner, SshRunner, VmSshSandboxBackend
from .catalog import find_option, summarize_specs
from .client import ThunderComputeClient
from .config import ThunderSandboxConfig


BOOTSTRAP_SSH_TIMEOUT_SECONDS = 900
ACTIVE_INSTANCE_STATUSES = frozenset({"running"})
LIVE_INSTANCE_STATUSES = frozenset({"starting", "running"})
TERMINAL_INSTANCE_STATUSES = frozenset({"terminated", "terminating", "stopped", "failed"})

THUNDER_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)

BootstrapRunner = Callable[[list[str], str, int], "subprocess.CompletedProcess[str]"]


class ThunderComputeSandboxBackend(VmSshSandboxBackend):
    capabilities = BackendCapabilities(
        name="thunder_compute",
        requires_hardware_selection=True,
        configurable_resources=False,
    )

    def __init__(
        self,
        *,
        config: ThunderSandboxConfig | None = None,
        client: ThunderComputeClient | None = None,
        ssh_runner: SshRunner | None = None,
        ssh_input_runner: SshInputRunner | None = None,
        bootstrap_runner: BootstrapRunner | None = None,
        parachute_runner: SshRunner | None = None,
    ) -> None:
        super().__init__(
            ssh_runner=ssh_runner,
            ssh_input_runner=ssh_input_runner,
            parachute_runner=parachute_runner,
        )
        self._config = config
        self._client = client
        self._bootstrap_runner = bootstrap_runner or _run_bootstrap

    @property
    def config(self) -> ThunderSandboxConfig:
        if self._config is None:
            self._config = ThunderSandboxConfig.from_env()
        return self._config

    @property
    def client(self) -> ThunderComputeClient:
        if self._client is None:
            self._client = ThunderComputeClient(config=self.config.cloud)
        return self._client

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        instance_type = (request.instance_type or self.config.instance_type_name or "").strip()
        if not instance_type:
            raise BackendValidationError(
                "Thunder Compute requires an instance_type. Call sandbox.options, "
                "or sandbox.request without an instance_type, to see available specs."
            )
        if not request.management_public_key or not request.management_key_path:
            raise BackendValidationError("Thunder Compute requires a management SSH key")
        _call(on_phase, "checking_capacity", instance_type)
        option = self._resolve_option(instance_type=instance_type, requested_gpu=request.gpu)

        instance_id = ""
        instance_uuid = ""
        try:
            _call(on_phase, "creating", instance_type)
            created = self.client.create_instance(
                cpu_cores=int(option["vcpus"]),
                disk_size_gb=int(option["storage_gib"]),
                gpu_type=str(option["gpu_type"]),
                mode=str(option["mode"]),
                num_gpus=int(option["gpu_count"]),
                template=str(option.get("template") or self.config.template),
                public_key=request.management_public_key,
            )
            instance_id = str(created["identifier"])
            instance_uuid = str(created.get("uuid") or instance_id)
            _call(on_created, instance_id, instance_uuid)

            _call(on_phase, "connecting", "waiting for running instance and ssh")
            instance = self._wait_for_running_instance(
                instance_id=instance_id, instance_uuid=instance_uuid, on_phase=on_phase
            )
            host = str(instance.get("ip") or "")
            port = int(instance.get("port") or 22)
            if not host:
                raise BackendUnavailableError("Thunder instance became running without a public IP")

            workdir = request.remote_workdir or remote_experiment_dir(
                experiment_id=request.experiment_id, root=self.config.remote_root
            )
            _call(on_phase, "bootstrapping", "installing sandbox ssh wrapper")
            self._bootstrap_vm(
                host=host,
                port=port,
                request=request,
                workdir=workdir,
                on_phase=on_phase,
            )
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
                dashboards={},
                gpu=str(option.get("gpu") or request.gpu or ""),
                cpu=float(option["vcpus"]),
                memory=int(option.get("memory_gib") or 0) * 1024 or None,
                instance_type=instance_type,
                region="",
                price_usd_per_hour=float(option.get("price_usd_per_hour") or 0.0),
            )
        except Exception:
            if instance_id:
                try:
                    self.client.delete_instance(instance_id)
                except Exception:  # noqa: BLE001
                    pass
            raise

    def is_alive(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            instance = self._instance_by_id(sandbox_id)
        except Exception:  # noqa: BLE001
            return False
        return _status(instance) in LIVE_INSTANCE_STATUSES

    def terminate(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            self.client.delete_instance(sandbox_id)
        except Exception:  # noqa: BLE001
            return False
        return True

    def local_dashboard_ports(self) -> dict[str, int]:
        return dict(DASHBOARD_PORTS)

    def sandbox_environment(self) -> dict[str, Any]:
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

    def health(self) -> dict[str, Any]:
        try:
            self.client.list_specs()
            return {"ok": True, "backend": "thunder_compute"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": "thunder_compute", "error": str(exc)}

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        summary = summarize_specs(
            self.client.list_specs(),
            pricing=self.client.pricing(),
            template=self.config.template,
            gpu=gpu,
        )
        options = summary["instance_types"]
        return {
            "provider": "thunder_compute",
            "selection_required": True,
            "select_with": "instance_type",
            "reason": (
                "Thunder Compute exposes fixed GPU specs by instance_type; pick "
                "one option rather than cpu/memory. Region selection is not exposed."
            ),
            "regions": [],
            "count": len(options),
            "options": options,
        }

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        marker = f"research-plugin-mgmt-{sandbox_uid or experiment_id}"
        try:
            instances = self.client.list_instances()
        except Exception:  # noqa: BLE001
            return None
        for fallback_id, row in instances.items():
            if _status(row) not in LIVE_INSTANCE_STATUSES:
                continue
            if _contains_key_comment(row, marker):
                return str(row.get("id") or row.get("identifier") or fallback_id)
        return None

    def _resolve_option(self, *, instance_type: str, requested_gpu: str | None) -> dict[str, Any]:
        summary = summarize_specs(
            self.client.list_specs(),
            pricing=self.client.pricing(),
            template=self.config.template,
        )
        option = find_option(summary, instance_type=instance_type)
        if option is None:
            offered = ", ".join(
                str(item.get("instance_type") or "")
                for item in summary.get("instance_types", [])
            ) or "(none)"
            raise BackendValidationError(
                f"Thunder Compute instance type is not currently offered: {instance_type}. "
                f"Currently offered: {offered}."
            )
        if requested_gpu:
            haystack = " ".join(
                str(option.get(key) or "")
                for key in ("instance_type", "gpu", "gpu_type")
            ).upper()
            if requested_gpu.upper() not in haystack:
                raise BackendValidationError(
                    f"requested gpu {requested_gpu} does not match Thunder instance "
                    f"type {instance_type} ({option.get('gpu') or 'unknown GPU'})"
                )
        return option

    def _wait_for_running_instance(
        self, *, instance_id: str, instance_uuid: str, on_phase: OnPhase | None = None
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_status = ""
        while time.monotonic() < deadline:
            instance = self._instance_by_id(instance_id, instance_uuid=instance_uuid)
            last_status = _status(instance)
            _call(on_phase, "connecting", f"Thunder instance status: {last_status or 'unknown'}")
            if last_status in ACTIVE_INSTANCE_STATUSES and instance.get("ip"):
                return instance
            if last_status in TERMINAL_INSTANCE_STATUSES:
                raise BackendUnavailableError(
                    f"Thunder instance {instance_id} reached terminal status {last_status}"
                )
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(
            f"Thunder instance {instance_id} did not become running before timeout "
            f"(last status: {last_status or 'unknown'})"
        )

    def _instance_by_id(
        self, instance_id: str, *, instance_uuid: str | None = None
    ) -> dict[str, Any]:
        instances = self.client.list_instances()
        row = instances.get(str(instance_id))
        if row is not None:
            return row
        if instance_uuid:
            for item in instances.values():
                if str(item.get("uuid") or item.get("name") or "") == instance_uuid:
                    return item
        raise BackendUnavailableError(f"Thunder instance not found: {instance_id}")

    def _bootstrap_vm(
        self,
        *,
        host: str,
        port: int,
        request: SandboxRequest,
        workdir: str,
        on_phase: OnPhase | None = None,
    ) -> None:
        script = build_thunder_bootstrap_script(
            public_key=request.public_key,
            management_public_key=request.management_public_key,
            experiment_id=request.experiment_id,
            workdir=workdir,
            sessions_dir=remote_sessions_dir(
                experiment_id=request.experiment_id, root=remote_root_of(workdir)
            ),
            sandbox_data_dir=self.config.sandbox_data_dir,
            tracking_env=request.tracking_env,
        )
        command = ssh_command(
            host=host,
            port=port,
            user=self.config.ssh_user,
            key_path=request.management_key_path,
            remote_command="sudo -n bash -s",
        )
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_error = ""
        while time.monotonic() < deadline:
            _call(on_phase, "bootstrapping", "running bootstrap over ssh")
            try:
                result = self._bootstrap_runner(
                    command,
                    script,
                    BOOTSTRAP_SSH_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            else:
                if result.returncode == 0:
                    self._wait_for_management_ssh(
                        host=host,
                        port=port,
                        key_path=request.management_key_path,
                        on_phase=on_phase,
                    )
                    return
                last_error = stderr_detail(result)
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(f"Thunder VM bootstrap failed: {last_error}")

    def _wait_for_management_ssh(
        self, *, host: str, port: int, key_path: str, on_phase: OnPhase | None = None
    ) -> None:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        last_error = ""
        while time.monotonic() < deadline:
            _call(on_phase, "bootstrapping", "waiting for management ssh")
            result = self._ssh_mgmt(
                host=host,
                port=port,
                key_path=key_path,
                remote_command="test -x /opt/rp/rec.sh && true",
            )
            if result.returncode == 0:
                return
            last_error = stderr_detail(result)
            time.sleep(self.config.poll_interval_seconds)
        raise BackendUnavailableError(f"Thunder management SSH never became ready: {last_error}")

    def _ssh_mgmt(
        self, *, host: str, port: int, key_path: str, remote_command: str
    ) -> subprocess.CompletedProcess[str]:
        command = ssh_command(
            host=host,
            port=port,
            user=MGMT_SSH_USER,
            key_path=key_path,
            remote_command=remote_command,
        )
        return self._ssh_runner(command)


def build_thunder_bootstrap_script(
    *,
    public_key: str,
    management_public_key: str,
    experiment_id: str,
    workdir: str,
    sessions_dir: str,
    sandbox_data_dir: str,
    tracking_env: Mapping[str, str] | None = None,
) -> str:
    apt_packages = " ".join(shlex.quote(pkg) for pkg in THUNDER_APT_PACKAGES)
    python_packages = " ".join(shlex.quote(pkg) for pkg in ML_PYTHON_PACKAGES)
    bootstrap_core = build_bootstrap_core(
        public_key=public_key,
        experiment_id=experiment_id,
        workdir=workdir,
        sessions_dir=sessions_dir,
        sandbox_data_dir=sandbox_data_dir,
        management_public_key=management_public_key,
        tracking_env=tracking_env,
        sshd_apply_command="systemctl reload ssh || systemctl reload sshd || service ssh reload || true",
    )
    return f"""#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

{bootstrap_core}
apt-get update
apt-get install -y --no-install-recommends {apt_packages}
ln -sf /usr/bin/fdfind /usr/local/bin/fd || true
python3 -m pip install --break-system-packages --upgrade pip uv || python3 -m pip install --user --upgrade pip uv || true
if [ -x /root/.local/bin/uv ]; then
  install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
fi
install_with_uv_or_pip() {{
  if command -v uv >/dev/null 2>&1; then
    uv pip install --system "$@" || python3 -m pip install --break-system-packages "$@"
  else
    python3 -m pip install --break-system-packages "$@"
  fi
}}
python3 -c 'import mlflow' >/dev/null 2>&1 || python3 -m pip install --break-system-packages --ignore-installed mlflow==2.18.0 || echo "[rp] mlflow install failed" >> /opt/rp/bootstrap.log
python3 -c 'import tensorboard' >/dev/null 2>&1 || python3 -m pip install --break-system-packages --ignore-installed tensorboard || echo "[rp] tensorboard install failed" >> /opt/rp/bootstrap.log
install_with_uv_or_pip {python_packages} || true
if id ubuntu >/dev/null 2>&1; then
  sudo -u ubuntu /opt/rp/start_dashboards.sh || true
else
  /opt/rp/start_dashboards.sh || true
fi
"""


def _status(instance: Mapping[str, Any]) -> str:
    return str(instance.get("status") or "").strip().lower()


def _contains_key_comment(instance: Mapping[str, Any], marker: str) -> bool:
    raw_keys = (
        instance.get("sshPublicKeys")
        or instance.get("ssh_public_keys")
        or instance.get("public_keys")
        or instance.get("publicKey")
        or instance.get("public_key")
    )
    if isinstance(raw_keys, str):
        return marker in raw_keys
    if isinstance(raw_keys, Mapping):
        return any(marker in str(value) for value in raw_keys.values())
    if isinstance(raw_keys, (list, tuple, set)):
        for item in raw_keys:
            if marker in str(item):
                return True
            if isinstance(item, Mapping) and any(marker in str(value) for value in item.values()):
                return True
    return False


def _run_bootstrap(
    command: list[str], script: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _call(cb: Any, *args: Any) -> None:
    if cb is not None:
        cb(*args)


def build_thunder_compute_sandbox_backend(
    *, repo_root: Path | None = None, **_kwargs: Any
) -> ThunderComputeSandboxBackend:
    return ThunderComputeSandboxBackend()
