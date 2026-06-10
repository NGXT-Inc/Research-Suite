"""Provider-neutral rsync transfer for SSH sandboxes."""

from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .sync_dirs import ARTIFACTS_TO_KEEP_DIRNAME


# Sandboxes ship rsync 3.x. Apple's bundled /usr/bin/rsync is 2.6.9
# (protocol 29) and cannot reliably transfer with a 3.x peer over the `-az`
# stream — the negotiation breaks with "unexpected tag" / "connection
# unexpectedly closed" protocol errors. We therefore resolve a *modern* rsync
# and refuse to spawn the ancient one with a clear, actionable error.
RSYNC_BIN_ENV = "RESEARCH_PLUGIN_RSYNC_BIN"
MIN_RSYNC_VERSION: tuple[int, int, int] = (3, 0, 0)

# Well-known locations for a Homebrew/MacPorts/Linux modern rsync, checked
# before falling back to PATH (a launchd-spawned backend may not inherit a
# PATH that includes /opt/homebrew/bin).
_MODERN_RSYNC_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/bin/rsync",
    "/usr/local/bin/rsync",
    "/opt/local/bin/rsync",
)


@dataclass(frozen=True)
class RsyncBinary:
    path: str
    version: tuple[int, ...]

    @property
    def is_modern(self) -> bool:
        return bool(self.version) and self.version >= MIN_RSYNC_VERSION

    @property
    def version_str(self) -> str:
        return ".".join(str(p) for p in self.version) if self.version else "unknown"


def _probe_version(path: str) -> tuple[int, ...]:
    try:
        proc = subprocess.run(
            [path, "--version"], text=True, capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    match = re.search(r"version\s+(\d+)\.(\d+)(?:\.(\d+))?", proc.stdout)
    if not match:
        return ()
    return tuple(int(g) for g in match.groups(default="0"))


@functools.lru_cache(maxsize=1)
def resolve_rsync() -> RsyncBinary:
    """Locate the best available rsync, preferring a modern (>= 3.0) build.

    Resolution order: explicit ``RESEARCH_PLUGIN_RSYNC_BIN`` override, then the
    well-known modern locations, then PATH. Apple's 2.6.9 is only returned as a
    last resort so callers can raise a precise "too old" error instead of the
    cryptic protocol failure it produces against 3.x sandboxes.
    """
    override = os.environ.get(RSYNC_BIN_ENV)
    if override:
        return RsyncBinary(path=override, version=_probe_version(override))

    candidates = list(_MODERN_RSYNC_CANDIDATES)
    on_path = shutil.which("rsync")
    if on_path:
        candidates.append(on_path)

    seen: set[str] = set()
    fallback: RsyncBinary | None = None
    for cand in candidates:
        if cand in seen or not os.path.exists(cand):
            continue
        seen.add(cand)
        binary = RsyncBinary(path=cand, version=_probe_version(cand))
        if binary.is_modern:
            return binary
        if fallback is None:
            fallback = binary
    return fallback or RsyncBinary(
        path="/usr/bin/rsync", version=_probe_version("/usr/bin/rsync")
    )


def _rsync_too_old_error(binary: RsyncBinary) -> RuntimeError:
    return RuntimeError(
        "local rsync is too old for sandbox sync: "
        f"{binary.path} is rsync {binary.version_str} (protocol 29). "
        "It cannot transfer with the sandbox's rsync 3.x — this is what "
        "produces the 'unexpected tag' / 'connection unexpectedly closed' "
        "errors. Install a modern rsync (`brew install rsync`) or point "
        f"{RSYNC_BIN_ENV} at an rsync >= {'.'.join(map(str, MIN_RSYNC_VERSION))}."
    )


DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".ipynb_checkpoints/",
    "node_modules/",
    ".cache/",
    "*.parquet",
    "*.arrow",
    "*.feather",
    "*.pt",
    "*.pth",
    "*.ckpt",
    "*.safetensors",
    "*.bin",
    "*.onnx",
    "*.h5",
    "*.npy",
    "*.npz",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
)


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SshRsyncResult:
    pulled: int
    duration_seconds: float
    local_dir: str
    remote_dir: str
    command_count: int
    stdout: str
    stderr: str
    direction: str = "pull"

    def as_dict(self) -> dict:
        return {
            "provider": "ssh_rsync",
            "direction": self.direction,
            "pulled": self.pulled,
            "duration_seconds": round(self.duration_seconds, 3),
            "local_dir": self.local_dir,
            "remote_dir": self.remote_dir,
            "command_count": self.command_count,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "conflicts": 0,
        }


class SshRsyncSyncer:
    def __init__(self, *, runner: Runner | None = None) -> None:
        # A custom runner is the test seam: when one is injected we never spawn
        # a real rsync, so the binary version gate is skipped.
        self._custom_runner = runner is not None
        self.runner = runner or self._run

    def _ensure_rsync_usable(self) -> None:
        if self._custom_runner:
            return
        binary = resolve_rsync()
        if not binary.is_modern:
            raise _rsync_too_old_error(binary)

    def sync(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        key_path: Path,
        remote_sync_dir: str,
        local_sync_dir: Path,
    ) -> SshRsyncResult:
        if not ssh_host or not ssh_port:
            raise RuntimeError("missing SSH endpoint for rsync")
        if not key_path.exists():
            raise RuntimeError(f"missing SSH key for rsync: {key_path}")
        self._ensure_rsync_usable()
        remote_sync_dir = remote_sync_dir.rstrip("/") or "/workspace/synced"
        local_sync_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        commands = [
            (
                self._pull_command(
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    ssh_user=ssh_user,
                    key_path=key_path,
                    remote_dir=remote_sync_dir,
                    local_dir=local_sync_dir,
                    max_size="100m",
                    excludes=DEFAULT_EXCLUDES + (f"{ARTIFACTS_TO_KEEP_DIRNAME}/",),
                ),
                False,
            ),
            (
                self._pull_command(
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    ssh_user=ssh_user,
                    key_path=key_path,
                    remote_dir=f"{remote_sync_dir}/{ARTIFACTS_TO_KEEP_DIRNAME}",
                    local_dir=local_sync_dir / ARTIFACTS_TO_KEEP_DIRNAME,
                    max_size="5g",
                    excludes=(),
                ),
                True,
            ),
        ]
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        pulled = 0
        ran = 0
        for command, optional in commands:
            result = self.runner(command)
            ran += 1
            stdout_parts.append(result.stdout or "")
            stderr_parts.append(result.stderr or "")
            if result.returncode != 0:
                # rsync returns 23 when one source path does not exist; tolerate
                # that only for the optional artifacts_to_keep pass.
                if not optional or result.returncode != 23:
                    raise RuntimeError(
                        f"rsync failed with exit {result.returncode}: {(result.stderr or '').strip()}"
                    )
            pulled += _count_changed(result.stdout or "")
        return SshRsyncResult(
            pulled=pulled,
            duration_seconds=time.monotonic() - start,
            local_dir=str(local_sync_dir),
            remote_dir=remote_sync_dir,
            command_count=ran,
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
            direction="pull",
        )

    def push_initial(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        key_path: Path,
        remote_sync_dir: str,
        local_sync_dir: Path,
    ) -> SshRsyncResult:
        if not ssh_host or not ssh_port:
            raise RuntimeError("missing SSH endpoint for rsync")
        if not key_path.exists():
            raise RuntimeError(f"missing SSH key for rsync: {key_path}")
        self._ensure_rsync_usable()
        remote_sync_dir = remote_sync_dir.rstrip("/") or "/workspace/synced"
        local_sync_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        commands = [
            (
                self._push_command(
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    ssh_user=ssh_user,
                    key_path=key_path,
                    remote_dir=remote_sync_dir,
                    local_dir=local_sync_dir,
                    max_size="100m",
                    excludes=DEFAULT_EXCLUDES + (f"{ARTIFACTS_TO_KEEP_DIRNAME}/",),
                ),
                False,
            ),
            (
                self._push_command(
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    ssh_user=ssh_user,
                    key_path=key_path,
                    remote_dir=f"{remote_sync_dir}/{ARTIFACTS_TO_KEEP_DIRNAME}",
                    local_dir=local_sync_dir / ARTIFACTS_TO_KEEP_DIRNAME,
                    max_size="5g",
                    excludes=(),
                ),
                True,
            ),
        ]
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        pushed = 0
        ran = 0
        for command, optional in commands:
            result = self.runner(command)
            ran += 1
            stdout_parts.append(result.stdout or "")
            stderr_parts.append(result.stderr or "")
            if result.returncode != 0:
                if not optional or result.returncode != 23:
                    raise RuntimeError(
                        f"rsync failed with exit {result.returncode}: {(result.stderr or '').strip()}"
                    )
            pushed += _count_changed(result.stdout or "")
        return SshRsyncResult(
            pulled=pushed,
            duration_seconds=time.monotonic() - start,
            local_dir=str(local_sync_dir),
            remote_dir=remote_sync_dir,
            command_count=ran,
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
            direction="push",
        )

    def _pull_command(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        key_path: Path,
        remote_dir: str,
        local_dir: Path,
        max_size: str,
        excludes: tuple[str, ...],
    ) -> list[str]:
        local_dir.mkdir(parents=True, exist_ok=True)
        ssh = (
            f"ssh -i {shlex.quote(os.fspath(key_path))} -p {ssh_port} -o BatchMode=yes "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            "-o ConnectTimeout=10"
        )
        command = [
            resolve_rsync().path,
            "-az",
            "--delete",
            "--prune-empty-dirs",
            "--itemize-changes",
            "--out-format=%n",
            f"--max-size={max_size}",
            "-e",
            ssh,
        ]
        for pattern in excludes:
            command.extend(["--exclude", pattern])
        command.extend([
            f"{ssh_user}@{ssh_host}:{remote_dir.rstrip('/')}/",
            os.fspath(local_dir) + "/",
        ])
        return command

    def _push_command(
        self,
        *,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        key_path: Path,
        remote_dir: str,
        local_dir: Path,
        max_size: str,
        excludes: tuple[str, ...],
    ) -> list[str]:
        local_dir.mkdir(parents=True, exist_ok=True)
        ssh = (
            f"ssh -i {shlex.quote(os.fspath(key_path))} -p {ssh_port} -o BatchMode=yes "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            "-o ConnectTimeout=10"
        )
        command = [
            resolve_rsync().path,
            "-az",
            "--delete",
            "--prune-empty-dirs",
            "--itemize-changes",
            "--out-format=%n",
            f"--max-size={max_size}",
            "-e",
            ssh,
        ]
        for pattern in excludes:
            command.extend(["--exclude", pattern])
        command.extend([
            os.fspath(local_dir) + "/",
            f"{ssh_user}@{ssh_host}:{remote_dir.rstrip('/')}/",
        ])
        return command

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, text=True, capture_output=True, timeout=600)


def _count_changed(stdout: str) -> int:
    count = 0
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith("/") or line.startswith(".d"):
            continue
        count += 1
    return count
