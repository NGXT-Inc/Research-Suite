"""Modal sandbox backend: procure SSH-wired sandboxes, no job protocol.

Implements the SandboxBackend protocol. The registry (SandboxService) decides
reuse-vs-create policy; this layer only knows how to:

  - acquire(): create one Modal sandbox wired for SSH over an unencrypted tunnel,
    with the project Volume mounted and the agent's public key authorized;
  - is_alive(): poll a sandbox by id;
  - terminate(): stop a sandbox by id;
  - read_transcript(): read the experiment's terminal transcript, live from the
    sandbox first and from the committed Volume as a fallback;
  - health(): is Modal reachable.

The Volume + bidirectional sync subsystem is reused unchanged from the job-era
backend (see sync/sync.md).
"""

from __future__ import annotations

import base64
import shlex
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from ...errors import BackendUnavailableError, BackendValidationError
from ...types import (
    BackendCapabilities,
    OnCreated,
    OnPhase,
    ProvisionedSandbox,
    SandboxRequest,
)
from .config import ModalConfig
from ._sandbox_ops import maybe_await, read_stream, wait_process
from .sync import BaselineStore, SyncEngine, SyncPoller


ActivityHook = Callable[[str, dict[str, Any]], None]
ShouldPollProject = Callable[[str], bool]

SESSIONS_DIR_NAME = ".research_plugin_sessions"
TRANSCRIPT_FILENAME = "transcript.log"
TRANSCRIPT_TAIL_DEFAULT = 50_000


# Entrypoint baked into every sandbox image. Authorizes the registry-owned key,
# generates host keys, writes an sshd_config whose ForceCommand is the transcript
# wrapper, then execs sshd in the foreground (which keeps the container alive).
BOOT_SCRIPT = r"""#!/usr/bin/env bash
set -eu
mkdir -p /root/.ssh && chmod 700 /root/.ssh
if [ -n "${RP_AUTHORIZED_KEY:-}" ]; then
  printf '%s\n' "$RP_AUTHORIZED_KEY" > /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi
# Persist the session env so the ForceCommand wrapper can read it (sshd does not
# pass the container environment through to forced commands).
{
  printf 'RP_WORKDIR=%s\n' "${RP_WORKDIR:-/workspace/repo}"
  printf 'RP_EXPERIMENT_ID=%s\n' "${RP_EXPERIMENT_ID:-unknown}"
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
# `ssh host 'cmd'`) to a per-experiment transcript on the mounted Volume while
# still streaming output back to the agent. Exit code is preserved.
REC_SCRIPT = r"""#!/usr/bin/env bash
[ -f /opt/rp/env ] && . /opt/rp/env
RP_WORKDIR="${RP_WORKDIR:-/workspace/repo}"
RP_EXPERIMENT_ID="${RP_EXPERIMENT_ID:-unknown}"
LOG_DIR="$RP_WORKDIR/.research_plugin_sessions/$RP_EXPERIMENT_ID"
LOG="$LOG_DIR/transcript.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
if [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
  { printf '\n[%s] $ %s\n' "$(ts)" "$SSH_ORIGINAL_COMMAND" >> "$LOG"; } 2>/dev/null || true
  cd "$RP_WORKDIR" 2>/dev/null || true
  bash -lc "$SSH_ORIGINAL_COMMAND" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  { printf '[%s] (exit %d)\n' "$(ts)" "$rc" >> "$LOG"; } 2>/dev/null || true
  sync "$RP_WORKDIR" 2>/dev/null || true
  exit "$rc"
else
  { printf '\n[%s] (interactive shell)\n' "$(ts)" >> "$LOG"; } 2>/dev/null || true
  cd "$RP_WORKDIR" 2>/dev/null || true
  exec bash -l
fi
"""


class ModalSandboxBackend:
    capabilities = BackendCapabilities(name="modal")

    def __init__(
        self,
        *,
        repo_root: Path,
        config: ModalConfig | None = None,
        modal_module: Any | None = None,
        activity: ActivityHook | None = None,
        sync_engine: SyncEngine | None = None,
        baseline: BaselineStore | None = None,
        poller_interval_seconds: float = 60.0,
        start_poller: bool = True,
        should_poll_project: ShouldPollProject | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config or ModalConfig.from_env()
        self.activity = activity
        self._modal = modal_module
        self._app = None
        self._base_image = None
        self._cuda_image = None
        self._lock = threading.Lock()
        self._volume_objects: dict[str, Any] = {}

        if sync_engine is not None:
            self.baseline = baseline if baseline is not None else sync_engine.baseline
            self.sync_engine = sync_engine
        else:
            sync_db = self.repo_root / ".research_plugin" / "modal" / "sync.sqlite"
            self.baseline = baseline or BaselineStore(db_path=sync_db)
            self.sync_engine = SyncEngine(
                repo_root=self.repo_root,
                baseline=self.baseline,
                volume_provider=self._provide_volume,
                volume_name_prefix=self.config.volume_name_prefix,
                volume_mount_path=self.config.remote_workdir,
                activity=self.activity,
            )
        self.poller = SyncPoller(
            engine=self.sync_engine,
            baseline=self.baseline,
            interval_seconds=poller_interval_seconds,
            activity=self.activity,
            should_sync_project=should_poll_project,
        )
        if start_poller:
            self.poller.start()

    # ---------- SandboxBackend protocol ----------

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase: OnPhase | None = None,
        on_created: OnCreated | None = None,
    ) -> ProvisionedSandbox:
        self._ensure_credentials()
        _call(on_phase, "syncing", "ensuring project volume")
        info = self.sync_engine.ensure_project_volume(project_id=request.project_id)
        volume_name = info["volume_name"]
        # Push current repo state to the Volume before the sandbox boots so the
        # agent sees up-to-date code/configs. This can be the slow step on a
        # large first-time delta — which is exactly why provisioning runs in the
        # background and the agent polls.
        _call(on_phase, "syncing", "pushing repo to volume")
        try:
            self.sync_engine.sync(project_id=request.project_id)
        except Exception:  # noqa: BLE001 — sync is best-effort at acquire time
            pass

        workdir = request.remote_workdir or self.config.remote_workdir
        volume = self._provide_volume(volume_name)
        modal = self._modal_module()
        image = self._image(cuda_devel=request.cuda_devel, image_packages=request.image_packages)
        app = self._get_app()
        secret = modal.Secret.from_dict(
            {
                "RP_AUTHORIZED_KEY": request.public_key,
                "RP_EXPERIMENT_ID": request.experiment_id,
                "RP_WORKDIR": workdir,
            }
        )
        name = _sandbox_name(request.experiment_id)
        kwargs: dict[str, Any] = {
            "app": app,
            "image": image,
            "timeout": int(request.time_limit),
            "workdir": workdir,
            "volumes": {workdir: volume},
            "unencrypted_ports": [22],
            "secrets": [secret],
            "cpu": request.cpu,
            "memory": int(request.memory),
            "name": name,
        }
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
            volume_name=volume_name,
            reused=False,
        )

    def find_sandbox_id(self, *, experiment_id: str) -> str | None:
        """Best-effort lookup of a sandbox we created for this experiment by name.

        Used by the registry to reconcile a provisioning row whose daemon-side
        job died before it recorded the sandbox id (e.g. a restart mid-provision)
        — the orphan still holds the deterministic name, so we can find and adopt
        or clean it up. Returns None if no such sandbox exists.
        """
        name = _sandbox_name(experiment_id)
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
        except Exception:  # noqa: BLE001
            return False

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
    ) -> str:
        limit = int(tail) if tail and tail > 0 else TRANSCRIPT_TAIL_DEFAULT
        rel_path = _transcript_rel_path(experiment_id)
        live = self._read_transcript_live(
            sandbox_id=sandbox_id,
            workdir=workdir,
            rel_path=rel_path,
            limit=limit,
        )
        if live:
            return live
        return self._read_transcript_volume(
            volume_name=volume_name,
            rel_path=rel_path,
            limit=limit,
        )

    def health(self) -> dict[str, Any]:
        try:
            self._ensure_credentials()
            self._get_app()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "name": "modal", "error": str(exc)}
        return {"ok": True, "name": "modal", "app": self.config.app_name}

    def on_project_created(self, *, project_id: str) -> None:
        self.sync_engine.ensure_project_volume(project_id=project_id)

    def shutdown(self) -> None:
        try:
            self.poller.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---------- transcript helpers ----------

    def _read_transcript_live(
        self, *, sandbox_id: str, workdir: str, rel_path: str, limit: int
    ) -> str:
        if not sandbox_id:
            return ""
        abs_path = PurePosixPath(workdir, rel_path).as_posix()
        command = (
            f"if [ -f {shlex.quote(abs_path)} ]; then "
            f"tail -c {int(limit)} {shlex.quote(abs_path)}; fi"
        )
        try:
            sandbox = self._sandbox_from_id(sandbox_id)
            process = sandbox.exec("bash", "-c", command, timeout=20)
            if wait_process(process) != 0:
                return ""
            return read_stream(getattr(process, "stdout", None))
        except Exception:  # noqa: BLE001
            return ""

    def _read_transcript_volume(self, *, volume_name: str, rel_path: str, limit: int) -> str:
        if not volume_name:
            return ""
        try:
            volume = self._provide_volume(volume_name)
            chunks: list[bytes] = []
            for chunk in volume.read_file(rel_path):
                chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
            data = b"".join(chunks)
            if len(data) > limit:
                data = data[-limit:]
            return data.decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        except Exception:  # noqa: BLE001
            return ""

    # ---------- modal helpers ----------

    def _provide_volume(self, volume_name: str) -> Any:
        volume = self._volume_objects.get(volume_name)
        if volume is None:
            self._ensure_credentials()
            modal = self._modal_module()
            try:
                volume = modal.Volume.from_name(volume_name, create_if_missing=True)
            except Exception as exc:  # noqa: BLE001
                raise BackendUnavailableError(
                    f"Modal volume is unavailable: {volume_name}: {exc}"
                ) from exc
            self._volume_objects[volume_name] = volume
        reload = getattr(volume, "reload", None)
        if callable(reload):
            try:
                reload()
            except Exception:  # noqa: BLE001
                pass
        return volume

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
                        modal.Image.debian_slim(python_version="3.11")
                        .apt_install("openssh-server", "ca-certificates", "curl")
                        .pip_install("uv")
                        .run_commands(
                            "uv pip install --system torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121",
                            "uv pip install --system transformers numpy matplotlib pandas scikit-learn modal",
                        )
                    )
        return self._base_image

    def _cuda_image_default(self) -> Any:
        if self._cuda_image is None:
            with self._lock:
                if self._cuda_image is None:
                    modal = self._modal_module()
                    self._cuda_image = self._with_ssh(
                        modal.Image.from_registry(
                            "nvidia/cuda:12.1.1-devel-ubuntu22.04",
                            add_python="3.11",
                        )
                        .apt_install("openssh-server", "ca-certificates", "curl")
                        .pip_install("uv")
                        .run_commands(
                            "uv pip install --system torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121",
                            "uv pip install --system transformers numpy matplotlib pandas scikit-learn ninja modal",
                        )
                    )
        return self._cuda_image

    def _with_ssh(self, image: Any) -> Any:
        """Bake the SSH entrypoint + transcript wrapper into the image."""
        return image.run_commands(
            "mkdir -p /opt/rp",
            _write_file_layer(BOOT_SCRIPT, "/opt/rp/boot.sh"),
            _write_file_layer(REC_SCRIPT, "/opt/rp/rec.sh"),
            "chmod +x /opt/rp/boot.sh /opt/rp/rec.sh",
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
    should_poll_project: ShouldPollProject | None = None,
) -> ModalSandboxBackend:
    return ModalSandboxBackend(
        repo_root=repo_root,
        config=ModalConfig.from_env(),
        activity=activity,
        should_poll_project=should_poll_project,
    )
