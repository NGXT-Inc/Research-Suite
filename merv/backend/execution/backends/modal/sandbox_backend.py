"""Modal sandbox backend: procure SSH-wired sandboxes, no job protocol.

Implements the SandboxBackend protocol. The registry (SandboxService) decides
reuse-vs-create policy; this layer only knows how to:

  - acquire(): create one Modal sandbox wired for SSH over an unencrypted tunnel,
    with the agent's public key authorized;
  - is_alive(): poll a sandbox by id;
  - terminate(): stop a sandbox by id;
  - read_transcript(): read the experiment's terminal transcript, live from the
    sandbox;
  - health(): is Modal reachable.
"""

from __future__ import annotations

import base64
import os
import shlex
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from backend.env import env_value
from backend.execution.bootstrap_tools import (
    BASELINE_APT_PACKAGES,
    ML_PYTHON_PACKAGES,
    REC_EXEC_CORE,
)
from backend.execution.run_receipts import (
    RP_RUN_PATH,
    RP_RUN_SCRIPT,
    parse_runs_listing,
    runs_listing_command,
)
from backend.execution.usage_metrics import (
    METRICS_EXEC_TIMEOUT,
    METRICS_SCRIPT,
    parse_metrics,
)
from ....sandbox.sandbox_backend import BackendUnavailableError, BackendValidationError
from ...sync_dirs import remote_experiment_dir, remote_root_of, remote_sessions_dir
from ...transcript_wire import (
    TRANSCRIPT_TAIL_DEFAULT,
    parse_transcript_tail,
    transcript_tail_command,
)


MODAL_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)
from ....sandbox.sandbox_backend import (
    BackendCapabilities,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxBackendBase,
    SandboxRequest,
    TranscriptTail,
)
from .config import COMPUTE_TIERS, DEFAULT_GPU, VALID_GPUS, ModalConfig
from ._sandbox_ops import ensure_remote_dir, exec_checked, maybe_await, read_stream, wait_process


ActivityHook = Callable[[str, dict[str, Any]], None]

SESSIONS_DIR_NAME = ".research_plugin_sessions"
TRANSCRIPT_FILENAME = "transcript.log"

# The usage sampler script + parser live in backend/execution/usage_metrics.py,
# shared with the Lambda backend (which runs the same probes over plain SSH).
# Here it runs via `sandbox.exec` (control-plane exec, so it bypasses the sshd
# ForceCommand transcript wrapper — same as the transcript reader).


# Entrypoint baked into every sandbox image. Authorizes the registry-owned key,
# generates host keys, writes an sshd_config whose ForceCommand is the transcript
# wrapper, then execs sshd in the foreground (which keeps the container alive).
BOOT_SCRIPT = r"""#!/usr/bin/env bash
set -eu
RP_EXPERIMENT_ID="${RP_EXPERIMENT_ID:-unknown}"
RP_WORKDIR="${RP_WORKDIR:-/workspace/$RP_EXPERIMENT_ID}"
RP_EXPERIMENT_DIR="${RP_EXPERIMENT_DIR:-$RP_WORKDIR}"
RP_SANDBOX_DATA_DIR="${RP_SANDBOX_DATA_DIR:-/workspace/data}"
mkdir -p "$RP_EXPERIMENT_DIR" "$RP_SANDBOX_DATA_DIR" "$RP_EXPERIMENT_DIR/artifacts_to_keep"
mkdir -p /root/.ssh && chmod 700 /root/.ssh
# Two keys, two duties (plan Phase 5, fixed decision 4): the user key is the
# data plane's (rsync, sbx dispatcher); the management key is the control
    # plane's transcript/metrics operations.
: > /root/.ssh/authorized_keys
if [ -n "${RP_AUTHORIZED_KEY:-}" ]; then
  printf '%s\n' "$RP_AUTHORIZED_KEY" >> /root/.ssh/authorized_keys
fi
if [ -n "${RP_MANAGEMENT_KEY:-}" ]; then
  printf '%s\n' "$RP_MANAGEMENT_KEY" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys
# Persist the session env so the ForceCommand wrapper can read it (sshd does not
# pass the container environment through to forced commands).
RP_SESSION_DIR="${RP_SESSION_DIR:-/workspace/.research_plugin_sessions/$RP_EXPERIMENT_ID}"
mkdir -p "$RP_SESSION_DIR" 2>/dev/null || true
{
  printf 'RP_WORKDIR=%q\n' "$RP_EXPERIMENT_DIR"
  printf 'RP_EXPERIMENT_DIR=%q\n' "$RP_EXPERIMENT_DIR"
  printf 'RP_EXPERIMENT_ID=%q\n' "$RP_EXPERIMENT_ID"
  printf 'RP_SANDBOX_DATA_DIR=%q\n' "$RP_SANDBOX_DATA_DIR"
  printf 'RP_DATASET_DIR=%q\n' "$RP_SANDBOX_DATA_DIR"
  printf 'RP_SESSION_DIR=%q\n' "$RP_SESSION_DIR"
  if [ -n "${HF_TOKEN:-}" ]; then
    printf 'HF_TOKEN=%q\n' "$HF_TOKEN"
    printf 'HUGGING_FACE_HUB_TOKEN=%q\n' "${HUGGING_FACE_HUB_TOKEN:-$HF_TOKEN}"
  fi
  if [ -n "${MLFLOW_TRACKING_PASSWORD:-}" ]; then
    printf 'MLFLOW_TRACKING_USERNAME=%q\n' "${MLFLOW_TRACKING_USERNAME:-rp-agent}"
    printf 'MLFLOW_TRACKING_PASSWORD=%q\n' "$MLFLOW_TRACKING_PASSWORD"
  fi
} > /opt/rp/env
mkdir -p /run/sshd
ssh-keygen -A >/dev/null 2>&1 || true
cat > /etc/ssh/sshd_config <<'EOF'
Port 22
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile /root/.ssh/authorized_keys
ForceCommand /opt/rp/rec.sh
PrintMotd no
AcceptEnv LANG LC_*
PidFile /run/sshd.pid
EOF
exec /usr/sbin/sshd -D -e
"""


# ForceCommand wrapper: records every SSH channel (interactive shell or
# `ssh host 'cmd'`) to a per-experiment transcript in the sessions dir while
# still streaming output back to the agent. Exit code is preserved.
REC_SCRIPT = r"""#!/usr/bin/env bash
[ -f /opt/rp/env ] && . /opt/rp/env
RP_EXPERIMENT_ID="${RP_EXPERIMENT_ID:-unknown}"
RP_WORKDIR="${RP_WORKDIR:-/workspace/$RP_EXPERIMENT_ID}"
RP_EXPERIMENT_DIR="${RP_EXPERIMENT_DIR:-$RP_WORKDIR}"
RP_SANDBOX_DATA_DIR="${RP_SANDBOX_DATA_DIR:-/workspace/data}"
RP_DATASET_DIR="${RP_DATASET_DIR:-$RP_SANDBOX_DATA_DIR}"
RP_SESSION_DIR="${RP_SESSION_DIR:-/workspace/.research_plugin_sessions/$RP_EXPERIMENT_ID}"
if [ -n "${HF_TOKEN:-}" ] && [ -z "${HUGGING_FACE_HUB_TOKEN:-}" ]; then
  HUGGING_FACE_HUB_TOKEN="$HF_TOKEN"
fi
export RP_WORKDIR RP_EXPERIMENT_DIR RP_EXPERIMENT_ID RP_SANDBOX_DATA_DIR RP_DATASET_DIR HF_TOKEN HUGGING_FACE_HUB_TOKEN MLFLOW_TRACKING_USERNAME MLFLOW_TRACKING_PASSWORD RP_SESSION_DIR
mkdir -p "$RP_EXPERIMENT_DIR" "$RP_SANDBOX_DATA_DIR" "$RP_EXPERIMENT_DIR/artifacts_to_keep" "$RP_SESSION_DIR" 2>/dev/null || true
LOG_DIR="$RP_SESSION_DIR"
LOG="$LOG_DIR/transcript.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
if [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
  # File-transfer protocols (rsync/scp/sftp) speak a binary protocol over stdio
  # and must bypass both the transcript tee and the tmux supervisor (which
  # detaches stdin).
  case "$SSH_ORIGINAL_COMMAND" in
    rsync\ --server*|*"sftp-server"*|internal-sftp*|scp\ -*)
      exec bash -lc "$SSH_ORIGINAL_COMMAND"
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


class ModalSandboxBackend(SandboxBackendBase):
    capabilities = BackendCapabilities(name="modal")

    def __init__(
        self,
        *,
        repo_root: Path,
        config: ModalConfig | None = None,
        modal_module: Any | None = None,
        activity: ActivityHook | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config or ModalConfig.from_env()
        self.activity = activity
        self._modal = modal_module
        self._app = None
        self._base_image = None
        self._cuda_image = None
        self._lock = threading.Lock()

    # ---------- SandboxBackend protocol ----------

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        self._ensure_credentials()
        workdir = request.remote_workdir or remote_experiment_dir(
            experiment_id=request.experiment_id, root=self.config.remote_root
        )
        sandbox_data_dir = self.config.sandbox_data_dir
        modal = self._modal_module()
        image = self._image(cuda_devel=request.cuda_devel, image_packages=request.image_packages)
        app = self._get_app()
        env = self._sandbox_env(
            public_key=request.public_key,
            management_public_key=request.management_public_key,
            experiment_id=request.experiment_id,
            workdir=workdir,
            sandbox_data_dir=sandbox_data_dir,
        )
        secrets = self._sandbox_secrets(modal)
        name = _sandbox_name(request.sandbox_uid or request.experiment_id)
        kwargs: dict[str, Any] = {
            "app": app,
            "image": image,
            "timeout": int(request.time_limit),
            "workdir": workdir,
            "unencrypted_ports": [22],
            "env": env,
            "cpu": request.cpu,
            "memory": int(request.memory),
            "name": name,
        }
        if secrets:
            kwargs["secrets"] = secrets
        if request.gpu:
            kwargs["gpu"] = request.gpu
        _call(on_phase, "creating", f"gpu={request.gpu or 'cpu'}")
        try:
            sandbox = modal.Sandbox.create("bash", "/opt/rp/boot.sh", **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(f"Modal sandbox create failed: {exc}") from exc

        sandbox_id = str(getattr(sandbox, "object_id", "") or "")
        # Past this point the sandbox EXISTS on Modal and holds the name. Any
        # failure (tunnel timeout, cancellation via a callback) must terminate it
        # before propagating, or it orphans and the deterministic name collides
        # on every later request.
        try:
            self._set_tags(
                sandbox=sandbox,
                tags={
                    "research_plugin": "true",
                    "research_plugin_role": "sandbox",
                    "experiment_id": request.experiment_id,
                    "project_id": request.project_id,
                },
            )
            # Hand the id back so the registry persists it before the slow tunnel
            # wait. May raise to cancel.
            _call(on_created, sandbox_id, name or "")
            _call(on_phase, "connecting", "waiting for ssh")
            host, port = self._ssh_endpoint(sandbox=sandbox)
        except BaseException:
            try:
                sandbox.terminate()
            except Exception:  # noqa: BLE001
                pass
            raise
        return ProvisionedSandbox(
            sandbox_id=sandbox_id,
            ssh_host=host,
            ssh_port=port,
            ssh_user="root",
            workdir=workdir,
            volume_name="",
            sync_dir=workdir,
            unsynced_dir=sandbox_data_dir,
            sandbox_data_dir=sandbox_data_dir,
            reused=False,
        )

    def find_sandbox_id(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> str | None:
        """Best-effort lookup of a sandbox we created for this experiment by name.

        Used by the registry to reconcile a provisioning row whose daemon-side
        job died before it recorded the sandbox id (e.g. a restart mid-provision)
        — the orphan still holds the deterministic name, so we can find and adopt
        or clean it up. Returns None if no such sandbox exists.
        """
        name = _sandbox_name(sandbox_uid or experiment_id)
        if not name:
            return None
        try:
            modal = self._modal_module()
            sandbox = modal.Sandbox.from_name(self.config.app_name, name)
            return str(getattr(sandbox, "object_id", "") or "") or None
        except Exception:  # noqa: BLE001 — not found / unreachable
            return None

    def is_alive(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            sandbox = self._sandbox_from_id(sandbox_id)
            poll = getattr(sandbox, "poll", None)
            if not callable(poll):
                return True
            return maybe_await(poll()) is None
        except Exception as exc:  # noqa: BLE001
            # modal.exception.NotFoundError = authoritatively gone; anything
            # else (auth, network, SDK) propagates so callers don't mistake an
            # outage for a dead sandbox.
            if "notfound" in type(exc).__name__.lower():
                return False
            raise

    def refresh_ssh_endpoint(self, *, sandbox_id: str) -> tuple[str, int] | None:
        """Re-read the live SSH tunnel endpoint for an existing sandbox.

        Lets the registry recover from a tunnel that moved without recreating
        the sandbox (is_alive only proves the control plane is up, not that the
        cached `rNNN.modal.host:port` still routes). Best-effort: returns None
        if the sandbox is gone or its tunnel can't be read right now.
        """
        if not sandbox_id:
            return None
        try:
            sandbox = self._sandbox_from_id(sandbox_id)
            return self._ssh_endpoint(sandbox=sandbox)
        except Exception:  # noqa: BLE001 — caller treats None as "couldn't refresh"
            return None

    def terminate(self, *, sandbox_id: str) -> bool:
        if not sandbox_id:
            return False
        try:
            sandbox = self._sandbox_from_id(sandbox_id)
        except Exception:  # noqa: BLE001
            return False
        ok = False
        try:
            sandbox.terminate()
            ok = True
        except Exception:  # noqa: BLE001
            pass
        try:
            detach = getattr(sandbox, "detach", None)
            if callable(detach):
                detach()
        except Exception:  # noqa: BLE001
            pass
        return ok

    def read_transcript(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        volume_name: str,
        workdir: str,
        tail: int | None = None,
        # SSH connection details are unused: Modal reads via control-plane exec.
        ssh_host: str = "",  # noqa: ARG002
        ssh_port: int = 0,  # noqa: ARG002
        ssh_user: str = "",  # noqa: ARG002
        key_path: str = "",  # noqa: ARG002
    ) -> TranscriptTail:
        limit = int(tail) if tail and tail > 0 else TRANSCRIPT_TAIL_DEFAULT
        live = self._read_transcript_live(
            sandbox_id=sandbox_id,
            experiment_id=experiment_id,
            workdir=workdir,
            limit=limit,
        )
        return live

    def sample_metrics(
        self,
        *,
        sandbox_id: str,
        # SSH connection details are unused: Modal samples via control-plane exec.
        ssh_host: str = "",  # noqa: ARG002
        ssh_port: int = 0,  # noqa: ARG002
        ssh_user: str = "",  # noqa: ARG002
        key_path: str = "",  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Sample live in-container usage (CPU/RAM/GPU) via a read-only exec.

        Returns a parsed gauge dict, or None when the sandbox is unreachable or
        the sampler produced nothing usable. Never raises — the registry treats
        a None as "metrics unavailable" and the UI hides the strip.
        """
        if not sandbox_id:
            return None
        try:
            sandbox = self._sandbox_from_id(sandbox_id)
            process = sandbox.exec("bash", "-c", METRICS_SCRIPT, timeout=METRICS_EXEC_TIMEOUT)
            if wait_process(process) != 0:
                return None
            output = read_stream(getattr(process, "stdout", None))
        except Exception:  # noqa: BLE001
            return None
        return parse_metrics(output)

    def read_runs(
        self,
        *,
        sandbox_id: str,
        workdir: str,
        # SSH connection details are unused: Modal lists via control-plane exec.
        ssh_host: str = "",  # noqa: ARG002
        ssh_port: int = 0,  # noqa: ARG002
        ssh_user: str = "",  # noqa: ARG002
        key_path: str = "",  # noqa: ARG002
    ) -> list[dict[str, Any]] | None:
        """List rp_run receipts under the workdir's .runs/ via a read-only exec.

        Returns parsed run records ([] when .runs is absent), or None when the
        sandbox is unreachable — the observer treats None as "no news".
        """
        if not sandbox_id or not workdir:
            return None
        try:
            sandbox = self._sandbox_from_id(sandbox_id)
            process = sandbox.exec(
                "bash",
                "-c",
                runs_listing_command(experiment_dir=workdir),
                timeout=METRICS_EXEC_TIMEOUT,
            )
            if wait_process(process) != 0:
                return None
            output = read_stream(getattr(process, "stdout", None))
        except Exception:  # noqa: BLE001
            return None
        return parse_runs_listing(output)

    def hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Static catalog: Modal lets the agent set gpu/cpu/memory independently.

        Unlike Lambda there is nothing to look up live — Modal composes the
        machine from the request — so this just advertises the menu of GPU types
        and compute tiers the agent can mix and match.
        """
        gpus = sorted(VALID_GPUS)
        if gpu:
            needle = gpu.strip().upper()
            gpus = [g for g in gpus if needle in g] or gpus
        return {
            "provider": "modal",
            "selection_required": False,
            "select_with": "gpu+cpu+memory",
            "reason": (
                "Modal composes the machine from the request: choose a gpu type "
                "(or omit for CPU-only) and set cpu cores / memory MiB directly."
            ),
            "gpus": gpus,
            "default_gpu": DEFAULT_GPU,
            "compute_tiers": COMPUTE_TIERS,
            "defaults": {"cpu": 2, "memory_mib": 8192},
            "notes": [
                "Omit gpu for a CPU-only sandbox.",
                "cpu is Modal CPU cores (1 core = 2 vCPUs).",
                "memory is requested sandbox memory in MiB.",
            ],
        }

    def health(self) -> dict[str, Any]:
        try:
            self._ensure_credentials()
            self._get_app()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "name": "modal", "error": str(exc)}
        return {"ok": True, "name": "modal", "app": self.config.app_name}

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

    def shutdown(self) -> None:
        return None

    # ---------- transcript helpers ----------

    def _read_transcript_live(
        self, *, sandbox_id: str, experiment_id: str, workdir: str, limit: int
    ) -> TranscriptTail:
        if not sandbox_id:
            return TranscriptTail(data=b"", total_bytes=0)
        base = workdir or remote_experiment_dir(
            experiment_id=experiment_id, root=self.config.remote_root
        )
        # Sessions live outside the experiment folder; legacy sandboxes
        # (pre-layout-change rows) kept them inside the synced workdir.
        abs_path = PurePosixPath(
            remote_sessions_dir(experiment_id=experiment_id, root=remote_root_of(base)),
            TRANSCRIPT_FILENAME,
        ).as_posix()
        legacy_path = PurePosixPath(base, _transcript_rel_path(experiment_id)).as_posix()
        command = transcript_tail_command(paths=[abs_path, legacy_path], limit=limit)
        try:
            sandbox = self._sandbox_from_id(sandbox_id)
            process = sandbox.exec("bash", "-c", command, timeout=20)
            if wait_process(process) != 0:
                return TranscriptTail(data=b"", total_bytes=0)
            return parse_transcript_tail(read_stream(getattr(process, "stdout", None)))
        except Exception:  # noqa: BLE001
            return TranscriptTail(data=b"", total_bytes=0)

    # ---------- modal helpers ----------

    def _sandbox_env(
        self,
        *,
        public_key: str,
        management_public_key: str,
        experiment_id: str,
        workdir: str,
        sandbox_data_dir: str,
    ) -> dict[str, str]:
        env = {
            "RP_AUTHORIZED_KEY": public_key,
            "RP_MANAGEMENT_KEY": management_public_key,
            "RP_EXPERIMENT_ID": experiment_id,
            "RP_WORKDIR": workdir,
            "RP_EXPERIMENT_DIR": workdir,
            "RP_SANDBOX_DATA_DIR": sandbox_data_dir,
            "RP_SESSION_DIR": remote_sessions_dir(
                experiment_id=experiment_id, root=remote_root_of(workdir)
            ),
        }
        return env

    def _sandbox_secrets(self, modal: Any) -> list[Any]:
        """Build Modal sandbox secrets from the daemon environment.

        The backend has already loaded the configured env file into
        ``os.environ``. Use Modal's local-environment helper instead of
        ``Secret.from_dotenv()`` so sandbox creation is independent of the
        daemon's current working directory.
        """
        secrets: list[Any] = []
        keys = ["HF_TOKEN"]
        if os.environ.get("HUGGING_FACE_HUB_TOKEN"):
            keys.append("HUGGING_FACE_HUB_TOKEN")
        if os.environ.get("HF_TOKEN"):
            secrets.append(modal.Secret.from_local_environ(keys))
        # MLflow credential pair for the authenticated hosted /mlflow route;
        # the brain env holds only the namespaced key, so map it explicitly.
        agent_key = env_value("MERV_MLFLOW_AGENT_KEY") or ""
        if agent_key:
            secrets.append(
                modal.Secret.from_dict(
                    {
                        "MLFLOW_TRACKING_USERNAME": "rp-agent",
                        "MLFLOW_TRACKING_PASSWORD": agent_key,
                    }
                )
            )
        return secrets

    def _ssh_endpoint(self, *, sandbox: Any) -> tuple[str, int]:
        get_tunnels = getattr(sandbox, "tunnels", None)
        if not callable(get_tunnels):
            raise BackendUnavailableError("Modal sandbox does not expose tunnels()")
        try:
            tunnels = maybe_await(get_tunnels())
            tunnel = tunnels[22]
            socket = getattr(tunnel, "tcp_socket", None)
            if not socket:
                raise BackendUnavailableError("Modal tunnel exposed no tcp_socket for port 22")
            return str(socket[0]), int(socket[1])
        except BackendUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(f"Modal SSH tunnel is unavailable: {exc}") from exc

    def _sandbox_from_id(self, sandbox_id: str) -> Any:
        modal = self._modal_module()
        return modal.Sandbox.from_id(sandbox_id)

    def _get_app(self) -> Any:
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = self._modal_module().App.lookup(
                        self.config.app_name,
                        create_if_missing=True,
                    )
        return self._app

    def _image(self, *, cuda_devel: bool, image_packages: tuple[str, ...]) -> Any:
        base = self._cuda_image_default() if cuda_devel else self._base_image_default()
        if image_packages:
            return base.pip_install(*image_packages)
        return base

    def _base_image_default(self) -> Any:
        if self._base_image is None:
            with self._lock:
                if self._base_image is None:
                    modal = self._modal_module()
                    self._base_image = self._with_ssh(
                        self._with_mlflow_client(
                            modal.Image.debian_slim(python_version="3.11")
                            .apt_install(*MODAL_APT_PACKAGES)
                            .pip_install("uv")
                            .run_commands(
                                "ln -sf /usr/bin/fdfind /usr/local/bin/fd || true",
                                "uv pip install --system torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121",
                                "uv pip install --system "
                                + " ".join((*ML_PYTHON_PACKAGES, "modal")),
                            )
                        )
                    )
        return self._base_image

    def _cuda_image_default(self) -> Any:
        if self._cuda_image is None:
            with self._lock:
                if self._cuda_image is None:
                    modal = self._modal_module()
                    self._cuda_image = self._with_ssh(
                        self._with_mlflow_client(
                            modal.Image.from_registry(
                                "nvidia/cuda:12.1.1-devel-ubuntu22.04",
                                add_python="3.11",
                            )
                            .apt_install(*MODAL_APT_PACKAGES)
                            .pip_install("uv")
                            .run_commands(
                                "ln -sf /usr/bin/fdfind /usr/local/bin/fd || true",
                                "uv pip install --system torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121",
                                "uv pip install --system "
                                + " ".join((*ML_PYTHON_PACKAGES, "ninja", "modal")),
                            )
                        )
                    )
        return self._cuda_image

    def _with_mlflow_client(self, image: Any) -> Any:
        """Layer in the MLflow client used with the centralized tracking URL."""
        return image.run_commands(
            "uv pip install --system mlflow==2.18.0",
        )

    def _with_ssh(self, image: Any) -> Any:
        """Bake the SSH entrypoint and transcript wrapper into the image."""
        return image.run_commands(
            "mkdir -p /opt/rp",
            _write_file_layer(BOOT_SCRIPT, "/opt/rp/boot.sh"),
            _write_file_layer(REC_SCRIPT, "/opt/rp/rec.sh"),
            _write_file_layer(RP_RUN_SCRIPT, RP_RUN_PATH),
            f"chmod +x /opt/rp/boot.sh /opt/rp/rec.sh {RP_RUN_PATH}",
            f"ln -sf {RP_RUN_PATH} /usr/local/bin/rp_run",
        )

    def _modal_module(self) -> Any:
        if self._modal is None:
            try:
                import modal  # type: ignore
            except ImportError as exc:
                raise BackendUnavailableError("modal SDK is not installed") from exc
            self._modal = modal
        return self._modal

    def _ensure_credentials(self) -> None:
        import os

        if not os.environ.get("MODAL_TOKEN_ID") or not os.environ.get("MODAL_TOKEN_SECRET"):
            raise BackendUnavailableError(
                "MODAL_TOKEN_ID / MODAL_TOKEN_SECRET are required for Modal execution"
            )

    def _set_tags(self, *, sandbox: Any, tags: Mapping[str, str]) -> None:
        set_tags = getattr(sandbox, "set_tags", None)
        if not callable(set_tags):
            return
        try:
            set_tags(dict(tags))
        except Exception:  # noqa: BLE001
            pass


def _call(cb: Any, *args: Any) -> None:
    """Invoke a progress callback if present; let it raise (to cancel)."""
    if cb is not None:
        cb(*args)


def _transcript_rel_path(experiment_id: str) -> str:
    safe = experiment_id or "unknown"
    return PurePosixPath(SESSIONS_DIR_NAME, safe, TRANSCRIPT_FILENAME).as_posix()


def _sandbox_name(experiment_id: str) -> str | None:
    if not experiment_id:
        return None
    import re

    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", experiment_id).strip("-")
    return f"rp-{safe or 'exp'}"[:63]


def _write_file_layer(content: str, path: str) -> str:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"printf %s '{encoded}' | base64 -d > {shlex.quote(path)}"


def build_modal_sandbox_backend(
    *,
    repo_root: Path,
    activity: ActivityHook | None = None,
) -> ModalSandboxBackend:
    return ModalSandboxBackend(
        repo_root=repo_root,
        config=ModalConfig.from_env(),
        activity=activity,
    )
