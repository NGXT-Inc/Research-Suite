"""Lambda Labs VM sandbox backend.

This backend provisions a Lambda Cloud VM and returns SSH details to the agent.
File sync is handled by SandboxService through provider-neutral SSH rsync; this
backend only prepares a normal developer shell with the tools agents expect.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import socket
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from backend.execution.bootstrap_tools import (
    LAMBDA_APT_PACKAGES,
    ML_PYTHON_PACKAGES,
    REC_EXEC_CORE,
)
from backend.execution.transfer_spec import (
    build_parachute_script,
    parse_parachute_receipt,
)
from backend.execution.usage_metrics import METRICS_SCRIPT, parse_metrics
from ...errors import BackendUnavailableError, BackendValidationError
from ...sync_dirs import remote_experiment_dir, remote_root_of, remote_sessions_dir
from ...types import (
    BackendCapabilities,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxBackendBase,
    SandboxRequest,
)
from .catalog import find_option, summarize_instance_types, to_agent_options
from .client import LambdaCloudClient
from .config import LambdaSandboxConfig


SESSIONS_DIR_NAME = ".research_plugin_sessions"
TRANSCRIPT_FILENAME = "transcript.log"
TRANSCRIPT_TAIL_DEFAULT = 50_000
# Sentinel prefix that rec.sh execs raw and UNRECORDED — recording such a read
# would tee the tail output back into the very log being read. Since the
# management-key switch (plan Phase 5) control-plane reads log in as the
# ForceCommand-exempt management principal instead, so the backend no longer
# sends this prefix; the rec.sh bypass is kept as a documented legacy/
# defensive path for sandboxes bootstrapped before the switch.
TRANSCRIPT_READ_PREFIX = "rp-transcript-read:"
# The dedicated management principal (plan Phase 5, fixed decision 4).
# Bootstrap creates this user with ONLY the management key authorized and an
# sshd Match block that swaps the global rec.sh ForceCommand for the raw
# pass-through below — so transcript polls and metrics samples are never
# recorded as commands, and the parachute runs even when the user's machine
# is gone. It gets passwordless sudo: the management channel's duties (tar
# the experiment dir at reap, read logs whatever login wrote them) need root,
# and the user key already grants root anyway — this is duty separation, not
# privilege separation.
MGMT_SSH_USER = "rpmgmt"
TRANSCRIPT_SSH_CONNECT_TIMEOUT = 10
TRANSCRIPT_READ_TIMEOUT_SECONDS = 30
# The parachute tars + uploads a whole experiment dir at reap time; give it
# its own generous subprocess budget (separate from the 30s transcript read).
PARACHUTE_SSH_TIMEOUT_SECONDS = 600
ACTIVE_INSTANCE_STATUSES = frozenset({"active"})
LIVE_INSTANCE_STATUSES = frozenset({"booting", "active", "unhealthy"})
DASHBOARD_PORTS: Mapping[str, int] = {"mlflow": 5000, "tensorboard": 6006}

SshRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


REC_SCRIPT = r"""#!/usr/bin/env bash
[ -f /opt/rp/env ] && . /opt/rp/env
# Credentials (HF_TOKEN, etc.) are NOT baked into user_data (plan Phase 9,
# risk 16). They are written post-boot to /opt/rp/secrets.env over the
# management channel and sourced here, so the cleartext token never lives in
# the provider's user_data blob or its on-disk copy.
[ -f /opt/rp/secrets.env ] && . /opt/rp/secrets.env
RP_EXPERIMENT_ID="${RP_EXPERIMENT_ID:-unknown}"
RP_WORKDIR="${RP_WORKDIR:-/workspace/$RP_EXPERIMENT_ID}"
RP_EXPERIMENT_DIR="${RP_EXPERIMENT_DIR:-$RP_WORKDIR}"
RP_SANDBOX_DATA_DIR="${RP_SANDBOX_DATA_DIR:-/workspace/data}"
RP_DATASET_DIR="${RP_DATASET_DIR:-$RP_SANDBOX_DATA_DIR}"
RP_DASH_DIR="${RP_DASH_DIR:-/workspace/.research_plugin_sessions/$RP_EXPERIMENT_ID}"
RP_TB_LOGDIR="${RP_TB_LOGDIR:-$RP_DASH_DIR/tb}"
MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-http://localhost:5000}"
export RP_WORKDIR RP_EXPERIMENT_DIR RP_EXPERIMENT_ID RP_SANDBOX_DATA_DIR RP_DATASET_DIR RP_DASH_DIR RP_TB_LOGDIR MLFLOW_TRACKING_URI
mkdir -p "$RP_EXPERIMENT_DIR" "$RP_SANDBOX_DATA_DIR" "$RP_EXPERIMENT_DIR/artifacts_to_keep" "$RP_DASH_DIR" 2>/dev/null || true
if [ -x /opt/rp/start_dashboards.sh ]; then
  /opt/rp/start_dashboards.sh >/dev/null 2>&1 || true
fi
LOG_DIR="$RP_DASH_DIR"
LOG="$LOG_DIR/transcript.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
if [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
  # File-transfer protocols (rsync/scp/sftp) speak a binary protocol over stdio.
  # The ForceCommand wrapper must hand them through untouched — teeing into the
  # transcript log corrupts the stream. Only interactive/command shells get
  # recorded. This is what lets the registry's rsync work once ForceCommand is
  # active (it is set up early in user_data now).
  case "$SSH_ORIGINAL_COMMAND" in
    rsync\ --server*|*"sftp-server"*|internal-sftp*|scp\ -*)
      exec bash -lc "$SSH_ORIGINAL_COMMAND"
      ;;
    rp-transcript-read:*)
      # Daemon-internal transcript poll (sandbox.terminal). Runs raw and
      # unrecorded: teeing it would feed the tail output back into the very
      # log it reads, growing the transcript on every poll.
      exec bash -c "${SSH_ORIGINAL_COMMAND#rp-transcript-read:}"
      ;;
  esac
  { printf '\n[%s] $ %s\n' "$(ts)" "$SSH_ORIGINAL_COMMAND" >> "$LOG"; } 2>/dev/null || true
  cd "$RP_EXPERIMENT_DIR" 2>/dev/null || true
""" + REC_EXEC_CORE + r"""
else
  { printf '\n[%s] (interactive shell)\n' "$(ts)" >> "$LOG"; } 2>/dev/null || true
  cd "$RP_EXPERIMENT_DIR" 2>/dev/null || true
  exec bash -l
fi
"""


# ForceCommand for the management principal: a raw pass-through. An explicit
# wrapper instead of `ForceCommand none` keeps the exemption auditable in
# /opt/rp and independent of the sshd build's support for the `none` argument.
MGMT_EXEC_SCRIPT = r"""#!/usr/bin/env bash
# research_plugin management channel (generated; plan Phase 5).
# The management principal is exempt from the rec.sh transcript wrapper:
# control-plane reads (transcript tail, metrics sampling) and the expiry
# parachute run here, raw and unrecorded — recording a transcript read would
# feed the tail output back into the very log it reads.
exec bash -lc "${SSH_ORIGINAL_COMMAND:-bash -l}"
"""


DASHBOARD_SCRIPT = r"""#!/usr/bin/env bash
set +e
[ -f /opt/rp/env ] && . /opt/rp/env
RP_EXPERIMENT_ID="${RP_EXPERIMENT_ID:-unknown}"
RP_DASH_DIR="${RP_DASH_DIR:-/workspace/.research_plugin_sessions/$RP_EXPERIMENT_ID}"
RP_MLFLOW_DB="$RP_DASH_DIR/mlflow.db"
RP_MLFLOW_ARTIFACTS="$RP_DASH_DIR/mlflow-artifacts"
RP_TB_LOGDIR="${RP_TB_LOGDIR:-$RP_DASH_DIR/tb}"
mkdir -p "$RP_MLFLOW_ARTIFACTS" "$RP_TB_LOGDIR" 2>/dev/null || true

pid_alive() {
  pid_file="$1"
  [ -s "$pid_file" ] || return 1
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

if python3 -c 'import mlflow' >/dev/null 2>&1; then
  if ! pid_alive "$RP_DASH_DIR/mlflow.pid"; then
    (
      cd /tmp || exit 0
      nohup python3 -m mlflow server \
        --host 127.0.0.1 --port 5000 \
        --backend-store-uri "sqlite:///$RP_MLFLOW_DB" \
        --artifacts-destination "file://$RP_MLFLOW_ARTIFACTS" \
        --serve-artifacts \
        >"$RP_DASH_DIR/mlflow.log" 2>&1 &
      echo $! > "$RP_DASH_DIR/mlflow.pid"
    )
  fi
else
  {
    printf '[%s] mlflow is not importable yet; dashboard not started\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    python3 -c 'import mlflow' 2>&1
  } >> "$RP_DASH_DIR/mlflow.log" 2>&1 || true
fi

if python3 -c 'import tensorboard' >/dev/null 2>&1; then
  if ! pid_alive "$RP_DASH_DIR/tensorboard.pid"; then
    (
      cd /tmp || exit 0
      nohup python3 -m tensorboard.main \
        --host 127.0.0.1 --port 6006 \
        --logdir "$RP_TB_LOGDIR" \
        >"$RP_DASH_DIR/tensorboard.log" 2>&1 &
      echo $! > "$RP_DASH_DIR/tensorboard.pid"
    )
  fi
fi
"""


class LambdaLabsSandboxBackend(SandboxBackendBase):
    # Lambda Labs sells fixed machine SKUs (GPU + vCPU + RAM bundled), so the
    # agent must pick an instance type — there are no independent cpu/memory
    # knobs. ``requires_hardware_selection`` makes SandboxService return a live
    # availability menu when ``sandbox.request`` arrives without an instance type.
    capabilities = BackendCapabilities(
        name="lambda_labs",
        requires_hardware_selection=True,
        configurable_resources=False,
    )

    def __init__(
        self,
        *,
        config: LambdaSandboxConfig | None = None,
        client: LambdaCloudClient | None = None,
        ssh_runner: SshRunner | None = None,
        parachute_runner: SshRunner | None = None,
    ) -> None:
        # Resolve config/client lazily so the daemon can boot (and report health)
        # with only an API key present — region/instance type are per-request,
        # and a missing key surfaces at call time as a clean health error rather
        # than crashing construction of the default backend.
        self._config = config
        self._client = client
        # Test seam: read_transcript shells out to ssh through this.
        self._ssh_runner = ssh_runner or _run_ssh
        # Separate seam (and timeout budget) for the parachute: tar + upload
        # of a whole experiment dir does not fit the transcript read's 30s.
        self._parachute_runner = parachute_runner or ssh_runner or _run_ssh_parachute

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
        instance_name = _sandbox_name(request.experiment_id)
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
        _call(on_phase, "checking_capacity", instance_type)
        region, specs = self._resolve_placement(
            instance_type=instance_type,
            region=(request.region or default_region or "").strip(),
            requested_gpu=request.gpu,
        )

        _call(on_phase, "registering_ssh_key", key_name)
        key_id = ""
        instance_id = ""
        try:
            key = self.client.add_ssh_key(name=key_name, public_key=request.public_key)
            key_id = str(key.get("id") or "")

            _call(on_phase, "creating", f"{instance_type} in {region}")
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
            _call(on_created, instance_id, instance_name)

            _call(on_phase, "connecting", "waiting for active instance and ssh")
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
                dashboards={},
                gpu=str(specs.get("gpu") or request.gpu or ""),
                cpu=float(specs["vcpus"]) if specs.get("vcpus") else None,
                memory=int(specs["memory_gib"]) * 1024 if specs.get("memory_gib") else None,
                instance_type=instance_type,
                region=region,
                price_usd_per_hour=float(specs.get("price_usd_per_hour") or 0.0),
            )
        except Exception:
            if instance_id:
                try:
                    self.client.terminate_instances([instance_id])
                except Exception:  # noqa: BLE001
                    pass
            if key_id:
                try:
                    self.client.delete_ssh_key(key_id)
                except Exception:  # noqa: BLE001
                    pass
            raise

    def is_alive(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            instance = self.client.get_instance(sandbox_id)
        except Exception:  # noqa: BLE001
            return False
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

    def read_transcript(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        volume_name: str,  # noqa: ARG002 — Lambda VMs have no volume
        workdir: str,
        tail: int | None = None,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",  # noqa: ARG002 — the management channel has its own principal
        key_path: str = "",
    ) -> str:
        """Tail the rec.sh transcript over the management SSH channel.

        ``key_path`` is the per-sandbox management private key (plan Phase 5,
        fixed decision 4); the read logs in as the dedicated management
        principal, which bootstrap exempts from the rec.sh ForceCommand — so
        polling the transcript is never itself recorded as a command and
        never depends on the user's machine. The row's data-plane ``ssh_user``
        is ignored.
        """
        if not sandbox_id or not ssh_host or not key_path:
            return ""
        limit = int(tail) if tail and tail > 0 else TRANSCRIPT_TAIL_DEFAULT
        base = workdir or remote_experiment_dir(
            experiment_id=experiment_id, root=self.config.remote_root
        )
        # Sessions live outside the experiment folder; legacy sandboxes
        # (pre-layout-change rows) kept them inside the synced workdir.
        log_path = PurePosixPath(
            remote_sessions_dir(experiment_id=experiment_id, root=remote_root_of(base)),
            TRANSCRIPT_FILENAME,
        ).as_posix()
        legacy_path = PurePosixPath(
            base, SESSIONS_DIR_NAME, experiment_id, TRANSCRIPT_FILENAME
        ).as_posix()
        remote_command = (
            f"if [ -f {shlex.quote(log_path)} ]; then "
            f"tail -c {limit} {shlex.quote(log_path)}; "
            f"elif [ -f {shlex.quote(legacy_path)} ]; then "
            f"tail -c {limit} {shlex.quote(legacy_path)}; fi"
        )
        command = [
            "ssh",
            "-i", key_path,
            "-p", str(int(ssh_port) or 22),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={TRANSCRIPT_SSH_CONNECT_TIMEOUT}",
            f"{MGMT_SSH_USER}@{ssh_host}",
            remote_command,
        ]
        try:
            result = self._ssh_runner(command)
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailableError(f"transcript read over SSH timed out: {exc}") from exc
        except OSError as exc:
            raise BackendUnavailableError(f"could not run ssh for transcript read: {exc}") from exc
        if result.returncode != 0:
            stderr_lines = (result.stderr or "").strip().splitlines()
            detail = stderr_lines[-1] if stderr_lines else "no stderr"
            raise BackendUnavailableError(
                f"transcript read over SSH failed (exit {result.returncode}): {detail}"
            )
        return result.stdout or ""

    def sample_metrics(
        self,
        *,
        sandbox_id: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",  # noqa: ARG002 — the management channel has its own principal
        key_path: str = "",
    ) -> dict[str, Any] | None:
        """Sample live VM usage (CPU/RAM/GPU) over the management SSH channel.

        Runs the shared sampler script as the ForceCommand-exempt management
        principal (``key_path`` is the management key, plan Phase 5), so the
        ~3s UI poll never spams the experiment transcript. On a dedicated VM
        the root cgroup / nvidia-smi probes gauge the whole machine, which is
        the number the user wants. Returns a parsed gauge dict, or None when
        the VM is unreachable or the sampler produced nothing usable. Never
        raises — the registry treats None as "metrics unavailable" and the UI
        hides the strip.
        """
        if not sandbox_id or not ssh_host or not key_path:
            return None
        command = [
            "ssh",
            "-i", key_path,
            "-p", str(int(ssh_port) or 22),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={TRANSCRIPT_SSH_CONNECT_TIMEOUT}",
            f"{MGMT_SSH_USER}@{ssh_host}",
            METRICS_SCRIPT,
        ]
        try:
            result = self._ssh_runner(command)
        except Exception:  # noqa: BLE001 — metrics are best-effort
            return None
        if result.returncode != 0:
            return None
        return parse_metrics(result.stdout or "")

    def run_parachute(
        self,
        *,
        sandbox_id: str,
        put_url: str,
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> dict[str, Any] | None:
        """Run the pre-installed parachute over the management channel.

        SSHes as the management principal with the management key
        (``key_path``) and executes ``/opt/rp/parachute.sh`` under sudo, so
        the tar can read every file in the experiment dir regardless of
        which login wrote it. Raises BackendUnavailableError on any failure
        so the reaper's parachute branch surfaces it loudly — a lost
        experiment dir must never fail silently (plan risk 9).
        """
        if not sandbox_id or not ssh_host or not key_path:
            raise BackendUnavailableError(
                "parachute needs the SSH endpoint and the management key"
            )
        remote_command = f"sudo -n bash /opt/rp/parachute.sh {shlex.quote(put_url)}"
        command = [
            "ssh",
            "-i", key_path,
            "-p", str(int(ssh_port) or 22),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={TRANSCRIPT_SSH_CONNECT_TIMEOUT}",
            f"{MGMT_SSH_USER}@{ssh_host}",
            remote_command,
        ]
        try:
            result = self._parachute_runner(command)
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailableError(f"parachute over SSH timed out: {exc}") from exc
        except OSError as exc:
            raise BackendUnavailableError(f"could not run ssh for parachute: {exc}") from exc
        if result.returncode != 0:
            stderr_lines = (result.stderr or "").strip().splitlines()
            detail = stderr_lines[-1] if stderr_lines else "no stderr"
            raise BackendUnavailableError(
                f"parachute over SSH failed (exit {result.returncode}): {detail}"
            )
        receipt = parse_parachute_receipt(result.stdout or "")
        if receipt is None:
            raise BackendUnavailableError("parachute produced no upload receipt")
        return receipt

    def sandbox_secrets(self) -> dict[str, str]:
        """The credentials to deliver to a fresh VM post-boot (HF tokens).

        Resolved from the control plane's env / secret store (HF_TOKEN +
        HUGGING_FACE_HUB_TOKEN); the control side hands these to write_secrets.
        Empty when none are configured.
        """
        return _sandbox_tokens()

    def write_secrets(
        self,
        *,
        sandbox_id: str,
        secrets: Mapping[str, str],
        ssh_host: str = "",
        ssh_port: int = 0,
        key_path: str = "",
    ) -> bool:
        """Deliver provider credentials post-boot over the management channel.

        Writes ``/opt/rp/secrets.env`` (sourced by rec.sh) as ``export NAME=...``
        lines, replacing the cleartext-in-user_data embed (plan Phase 9, risk
        16). The whole file is base64-encoded into the remote command so the
        token never appears as a process argument (it would otherwise show in
        ``ps``); the remote ``base64 -d`` reconstructs it into a 0600 file owned
        by root. Best-effort: returns False on any failure (the agent's HF
        downloads simply won't have the token) and never raises — provisioning
        must not fail because a token write was flaky.
        """
        if not sandbox_id or not ssh_host or not key_path or not secrets:
            return False
        body = "\n".join(
            f"export {name}={shlex.quote(value)}"
            for name, value in sorted(secrets.items())
            if value
        )
        if not body:
            return False
        payload_b64 = base64.b64encode((body + "\n").encode("utf-8")).decode("ascii")
        remote_command = (
            f"sudo -n bash -c "
            f"{shlex.quote(f'umask 077; printf %s {shlex.quote(payload_b64)} | base64 -d > /opt/rp/secrets.env; chmod 600 /opt/rp/secrets.env')}"
        )
        command = [
            "ssh",
            "-i", key_path,
            "-p", str(int(ssh_port) or 22),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={TRANSCRIPT_SSH_CONNECT_TIMEOUT}",
            f"{MGMT_SSH_USER}@{ssh_host}",
            remote_command,
        ]
        try:
            result = self._ssh_runner(command)
        except Exception:  # noqa: BLE001 — secret delivery is best-effort
            return False
        return result.returncode == 0

    def local_dashboard_ports(self) -> dict[str, int]:
        """Dashboard ports reachable only from inside the VM.

        The registry turns these into daemon-owned SSH local forwards and stores
        loopback URLs in the sandbox row. Modal does not use this path because it
        exposes native HTTPS dashboard tunnels.
        """
        return dict(DASHBOARD_PORTS)

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
            return {"ok": True, "backend": "lambda_labs"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": "lambda_labs", "error": str(exc)}

    def find_sandbox_id(self, *, experiment_id: str) -> str | None:
        name = _sandbox_name(experiment_id)
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
        return {
            "provider": "lambda_labs",
            "selection_required": True,
            "select_with": "instance_type",
            "reason": (
                "Lambda Labs bundles GPU, CPU, and RAM into fixed machine types; "
                "pick one instance_type rather than cpu/memory."
            ),
            "regions": summary["regions"],
            "count": len(options),
            "options": options,
        }

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
                raise BackendUnavailableError(
                    f"Lambda instance type {instance_type} has no current capacity in "
                    f"{region}. Regions with capacity now: {where}."
                )
            chosen = region
        else:
            if not available_regions:
                raise BackendUnavailableError(
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
                try:
                    self.client.delete_ssh_key(key_id)
                except Exception:  # noqa: BLE001
                    pass


def _sandbox_tokens() -> dict[str, str]:
    """Hugging Face credentials from the daemon env, for VM injection.

    Mirrors the Modal backend's secret injection: gated on HF_TOKEN, with
    HUGGING_FACE_HUB_TOKEN riding along when set.
    """
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        return {}
    tokens = {"HF_TOKEN": token}
    hub_token = os.environ.get("HUGGING_FACE_HUB_TOKEN", "")
    if hub_token:
        tokens["HUGGING_FACE_HUB_TOKEN"] = hub_token
    return tokens


def build_bootstrap_core(
    *,
    public_key: str,
    experiment_id: str,
    workdir: str,
    sessions_dir: str,
    sandbox_data_dir: str,
    management_public_key: str = "",
    tokens: Mapping[str, str] | None = None,
) -> str:
    """Phase 1 of the VM bootstrap: reachable, writable, both keys live.

    A shell fragment (no shebang) shared by ``build_user_data`` and the
    docker-simulated VM integration test, so what the test exercises is the
    bootstrap that ships. It creates the workspace tree, authorizes the user
    key (root + ubuntu), provisions the management principal with ONLY the
    management key, installs rec.sh + the dashboards script, and writes the
    sshd config (global rec.sh ForceCommand; Match-exempt management user).
    """
    public_key_b64 = base64.b64encode(public_key.encode("utf-8")).decode("ascii")
    rec_script_b64 = base64.b64encode(REC_SCRIPT.encode("utf-8")).decode("ascii")
    dashboard_script_b64 = base64.b64encode(DASHBOARD_SCRIPT.encode("utf-8")).decode("ascii")
    parachute_script_b64 = base64.b64encode(
        build_parachute_script().encode("utf-8")
    ).decode("ascii")
    env_lines = "\n".join(
        [
            f"RP_WORKDIR={shlex.quote(workdir)}",
            f"RP_EXPERIMENT_DIR={shlex.quote(workdir)}",
            f"RP_EXPERIMENT_ID={shlex.quote(experiment_id)}",
            f"RP_SANDBOX_DATA_DIR={shlex.quote(sandbox_data_dir)}",
            f"RP_DATASET_DIR={shlex.quote(sandbox_data_dir)}",
            f"RP_DASH_DIR={shlex.quote(sessions_dir)}",
            f"RP_TB_LOGDIR={shlex.quote(sessions_dir + '/tb')}",
            "MLFLOW_TRACKING_URI=http://localhost:5000",
            # NOTE (plan Phase 9, risk 16): credentials are deliberately NOT
            # embedded here. Cleartext in user_data lands in the provider's
            # instance metadata and on the VM's disk. Tokens are delivered
            # post-boot to /opt/rp/secrets.env over the management channel
            # (SandboxBackend.write_secrets) and sourced by rec.sh. ``tokens``
            # is accepted for signature stability but never written to user_data.
        ]
    )
    _ = tokens  # intentionally unused: never embedded in user_data (see above)
    mgmt_block = ""
    if management_public_key:
        mgmt_key_b64 = base64.b64encode(
            management_public_key.encode("utf-8")
        ).decode("ascii")
        mgmt_exec_b64 = base64.b64encode(
            MGMT_EXEC_SCRIPT.encode("utf-8")
        ).decode("ascii")
        mgmt_block = f"""
# Management principal (plan Phase 5, fixed decision 4): only the management
# key is authorized here, and the Match block below exempts it from the
# global rec.sh ForceCommand so control-plane reads are never recorded as
# commands. The Match append goes at the END of /etc/ssh/sshd_config, not in
# sshd_config.d — Ubuntu's Include sits at the top of the main file, so a
# Match opened in a conf.d snippet would swallow the distro directives that
# follow it (UsePAM and friends are illegal inside Match blocks).
useradd --create-home --shell /bin/bash {MGMT_SSH_USER} 2>/dev/null || true
mkdir -p /home/{MGMT_SSH_USER}/.ssh
printf '%s' {shlex.quote(mgmt_key_b64)} | base64 -d > /home/{MGMT_SSH_USER}/.ssh/authorized_keys
chown -R {MGMT_SSH_USER}:{MGMT_SSH_USER} /home/{MGMT_SSH_USER}/.ssh
chmod 700 /home/{MGMT_SSH_USER}/.ssh
chmod 600 /home/{MGMT_SSH_USER}/.ssh/authorized_keys
printf '{MGMT_SSH_USER} ALL=(ALL) NOPASSWD:ALL\\n' > /etc/sudoers.d/{MGMT_SSH_USER}
chmod 440 /etc/sudoers.d/{MGMT_SSH_USER}
printf '%s' {shlex.quote(mgmt_exec_b64)} | base64 -d > /opt/rp/mgmt_exec.sh
chmod +x /opt/rp/mgmt_exec.sh
cat >> /etc/ssh/sshd_config <<'RP_SSHD_MATCH'

Match User {MGMT_SSH_USER}
    ForceCommand /opt/rp/mgmt_exec.sh
RP_SSHD_MATCH
"""
    return f"""# === Phase 1: make the VM reachable + writable FAST, before the slow installs ===
# Create the workspace tree and authorize SSH up front so the registry's initial
# rsync — which fires the moment SSH is reachable — always lands in a writable
# directory. This used to run *after* a multi-minute Torch install, so the first
# push could race a not-yet-existent /workspace and fail.
mkdir -p /opt/rp /root/.ssh {shlex.quote(workdir)} {shlex.quote(sandbox_data_dir)} {shlex.quote(workdir)}/artifacts_to_keep {shlex.quote(sessions_dir)}
printf '%s' {shlex.quote(public_key_b64)} | base64 -d > /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
if id ubuntu >/dev/null 2>&1; then
  mkdir -p /home/ubuntu/.ssh
  printf '%s' {shlex.quote(public_key_b64)} | base64 -d >> /home/ubuntu/.ssh/authorized_keys
  chown -R ubuntu:ubuntu /home/ubuntu/.ssh {shlex.quote(workdir)} {shlex.quote(sandbox_data_dir)} {shlex.quote(sessions_dir)}
  chmod 700 /home/ubuntu/.ssh
  chmod 600 /home/ubuntu/.ssh/authorized_keys
fi
cat > /opt/rp/env <<'RP_ENV'
{env_lines}
RP_ENV
printf '%s' {shlex.quote(rec_script_b64)} | base64 -d > /opt/rp/rec.sh
printf '%s' {shlex.quote(dashboard_script_b64)} | base64 -d > /opt/rp/start_dashboards.sh
printf '%s' {shlex.quote(parachute_script_b64)} | base64 -d > /opt/rp/parachute.sh
chmod +x /opt/rp/rec.sh
chmod +x /opt/rp/start_dashboards.sh
chmod +x /opt/rp/parachute.sh
{mgmt_block}cat > /etc/ssh/sshd_config.d/99-research-plugin.conf <<'RP_SSHD'
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile .ssh/authorized_keys
ForceCommand /opt/rp/rec.sh
PrintMotd no
AcceptEnv LANG LC_*
RP_SSHD
systemctl restart ssh || systemctl restart sshd || service ssh restart || true
"""


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
    apt_packages = " ".join(shlex.quote(pkg) for pkg in LAMBDA_APT_PACKAGES)
    python_packages = " ".join(shlex.quote(pkg) for pkg in ML_PYTHON_PACKAGES)
    # Dashboard deps install one-at-a-time, only when missing, and with
    # --ignore-installed. The image ships Debian-owned Python packages without
    # RECORD files (Werkzeug 3.0.1 was the observed one); pip cannot uninstall
    # those, so any dependency upgrade that touches one aborts the whole
    # install — that is how mlflow silently went missing while the hint still
    # advertised it. --ignore-installed installs fresh copies into /usr/local
    # (which shadows the Debian dist-packages on sys.path) and never calls
    # uninstall at all. uv is skipped here: `uv pip install --system` refuses
    # PEP 668 externally-managed interpreters outright. tensorboard stays
    # unpinned (the preinstalled one is fine); mlflow keeps its pin.
    mlflow_package = shlex.quote("mlflow==2.18.0")
    bootstrap_core = build_bootstrap_core(
        public_key=public_key,
        experiment_id=experiment_id,
        workdir=workdir,
        sessions_dir=sessions_dir,
        sandbox_data_dir=sandbox_data_dir,
        management_public_key=management_public_key,
        tokens=tokens,
    )
    return f"""#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

{bootstrap_core}
# === Phase 2: heavy toolchain install (the VM is already usable by here) ===
apt-get update
apt-get install -y --no-install-recommends {apt_packages}
ln -sf /usr/bin/fdfind /usr/local/bin/fd || true
python3 -m pip install --break-system-packages --upgrade pip uv || python3 -m pip install --user --upgrade pip uv || true
if [ -x /root/.local/bin/uv ]; then
  install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
fi
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh || true
  if [ -x /root/.local/bin/uv ]; then
    install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
  fi
fi
install_with_uv_or_pip() {{
  if command -v uv >/dev/null 2>&1; then
    uv pip install --system "$@" || python3 -m pip install --break-system-packages "$@"
  else
    python3 -m pip install --break-system-packages "$@"
  fi
}}
python3 -c 'import mlflow' >/dev/null 2>&1 || python3 -m pip install --break-system-packages --ignore-installed {mlflow_package} || echo "[rp] mlflow install failed" >> /opt/rp/bootstrap.log
python3 -c 'import tensorboard' >/dev/null 2>&1 || python3 -m pip install --break-system-packages --ignore-installed tensorboard || echo "[rp] tensorboard install failed" >> /opt/rp/bootstrap.log
install_with_uv_or_pip torch torchvision torchaudio || true
install_with_uv_or_pip {python_packages} || true
# Dashboards write pids/logs into the sessions dir, which the daemon pulls
# over ubuntu-user rsync; start them as the SSH login user, never root —
# root-owned files there would break that pull (exit 23, permission denied).
if id ubuntu >/dev/null 2>&1; then
  sudo -u ubuntu /opt/rp/start_dashboards.sh || true
else
  /opt/rp/start_dashboards.sh || true
fi
"""


def _sandbox_name(experiment_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", experiment_id.lower()).strip("-")
    return f"rp-{safe or 'exp'}"[:60]


def _run_ssh(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, text=True, capture_output=True, timeout=TRANSCRIPT_READ_TIMEOUT_SECONDS
    )


def _run_ssh_parachute(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, text=True, capture_output=True, timeout=PARACHUTE_SSH_TIMEOUT_SECONDS
    )


def _call(cb: Any, *args: Any) -> None:
    if cb is not None:
        cb(*args)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_lambda_labs_sandbox_backend(*, repo_root: Path | None = None, **_kwargs: Any) -> LambdaLabsSandboxBackend:
    # Lazy: do not resolve credentials/region/instance type at construction so
    # the default backend can be built (and health-checked) with only an API key.
    return LambdaLabsSandboxBackend()
