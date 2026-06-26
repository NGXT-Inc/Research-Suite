"""Shared management-SSH operations for VM sandbox backends."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from backend.sandbox.sandbox_backend import BackendUnavailableError

from .sync_dirs import remote_experiment_dir, remote_root_of, remote_sessions_dir
from .usage_metrics import METRICS_SCRIPT, parse_metrics
from .vm_bootstrap import (
    MGMT_SSH_USER,
    SESSIONS_DIR_NAME,
    TRANSCRIPT_FILENAME,
    build_runtime_env,
)


TRANSCRIPT_TAIL_DEFAULT = 50_000
TRANSCRIPT_SSH_CONNECT_TIMEOUT = 10
TRANSCRIPT_READ_TIMEOUT_SECONDS = 30

SshRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
SshInputRunner = Callable[[list[str], str], "subprocess.CompletedProcess[str]"]


def read_transcript_via_mgmt_ssh(
    *,
    ssh_runner: SshRunner,
    sandbox_id: str,
    experiment_id: str,
    workdir: str,
    remote_root: str,
    ssh_host: str,
    ssh_port: int,
    key_path: str,
    tail: int | None = None,
) -> str:
    if not sandbox_id or not ssh_host or not key_path:
        return ""
    limit = int(tail) if tail and tail > 0 else TRANSCRIPT_TAIL_DEFAULT
    base = workdir or remote_experiment_dir(experiment_id=experiment_id, root=remote_root)
    log_path = PurePosixPath(
        remote_sessions_dir(experiment_id=experiment_id, root=remote_root_of(base)),
        TRANSCRIPT_FILENAME,
    ).as_posix()
    legacy_path = PurePosixPath(base, SESSIONS_DIR_NAME, experiment_id, TRANSCRIPT_FILENAME).as_posix()
    remote_command = (
        f"if [ -f {shlex.quote(log_path)} ]; then "
        f"tail -c {limit} {shlex.quote(log_path)}; "
        f"elif [ -f {shlex.quote(legacy_path)} ]; then "
        f"tail -c {limit} {shlex.quote(legacy_path)}; fi"
    )
    try:
        result = ssh_runner(
            ssh_command(
                host=ssh_host,
                port=int(ssh_port) or 22,
                user=MGMT_SSH_USER,
                key_path=key_path,
                remote_command=remote_command,
            )
        )
    except subprocess.TimeoutExpired as exc:
        raise BackendUnavailableError(f"transcript read over SSH timed out: {exc}") from exc
    except OSError as exc:
        raise BackendUnavailableError(f"could not run ssh for transcript read: {exc}") from exc
    if result.returncode != 0:
        raise BackendUnavailableError(
            f"transcript read over SSH failed (exit {result.returncode}): {stderr_detail(result)}"
        )
    return result.stdout or ""


def sample_metrics_via_mgmt_ssh(
    *,
    ssh_runner: SshRunner,
    sandbox_id: str,
    ssh_host: str,
    ssh_port: int,
    key_path: str,
) -> dict[str, Any] | None:
    if not sandbox_id or not ssh_host or not key_path:
        return None
    try:
        result = ssh_runner(
            ssh_command(
                host=ssh_host,
                port=int(ssh_port) or 22,
                user=MGMT_SSH_USER,
                key_path=key_path,
                remote_command=METRICS_SCRIPT,
            )
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    return parse_metrics(result.stdout or "")


def write_secrets_via_mgmt_ssh(
    *,
    ssh_runner: SshInputRunner,
    sandbox_id: str,
    secrets: Mapping[str, str],
    ssh_host: str,
    ssh_port: int,
    key_path: str,
) -> bool:
    if not sandbox_id or not ssh_host or not key_path or not secrets:
        return False
    body = "\n".join(
        f"export {name}={shlex.quote(value)}"
        for name, value in sorted(secrets.items())
        if value
    )
    if not body:
        return False
    remote_command = (
        "sudo -n bash -c "
        + shlex.quote("umask 077; cat > /opt/rp/secrets.env; chmod 600 /opt/rp/secrets.env")
    )
    try:
        result = ssh_runner(
            ssh_command(
                host=ssh_host,
                port=int(ssh_port) or 22,
                user=MGMT_SSH_USER,
                key_path=key_path,
                remote_command=remote_command,
            ),
            body + "\n",
        )
    except Exception:  # noqa: BLE001
        return False
    return result.returncode == 0


def retarget_via_mgmt_ssh(
    *,
    ssh_runner: SshInputRunner,
    sandbox_id: str,
    experiment_id: str,
    public_key: str,
    workdir: str,
    sandbox_data_dir: str,
    tracking_env: Mapping[str, str],
    ssh_host: str,
    ssh_port: int,
    key_path: str,
) -> bool:
    if not sandbox_id or not ssh_host or not key_path or not workdir:
        raise BackendUnavailableError("retarget needs the SSH endpoint and management key")
    sessions_dir = remote_sessions_dir(
        experiment_id=experiment_id, root=remote_root_of(workdir)
    )
    env_body = build_runtime_env(
        experiment_id=experiment_id,
        workdir=workdir,
        sessions_dir=sessions_dir,
        sandbox_data_dir=sandbox_data_dir,
        tracking_env=tracking_env,
    )
    mkdirs = " ".join(
        shlex.quote(path)
        for path in (workdir, f"{workdir}/artifacts_to_keep", sandbox_data_dir, sessions_dir)
        if path
    )
    pub = shlex.quote(public_key)
    remote_command = (
        "sudo -n bash -c "
        + shlex.quote(
            "umask 022; "
            f"mkdir -p {mkdirs} /root/.ssh; "
            "touch /root/.ssh/authorized_keys; chmod 700 /root/.ssh; "
            "chmod 600 /root/.ssh/authorized_keys; "
            f"pub={pub}; "
            'grep -qxF "$pub" /root/.ssh/authorized_keys '
            '|| printf "%s\\n" "$pub" >> /root/.ssh/authorized_keys; '
            "if id ubuntu >/dev/null 2>&1; then "
            "mkdir -p /home/ubuntu/.ssh; "
            "touch /home/ubuntu/.ssh/authorized_keys; "
            'grep -qxF "$pub" /home/ubuntu/.ssh/authorized_keys '
            '|| printf "%s\\n" "$pub" >> /home/ubuntu/.ssh/authorized_keys; '
            "chown -R ubuntu:ubuntu /home/ubuntu/.ssh; "
            "chmod 700 /home/ubuntu/.ssh; "
            "chmod 600 /home/ubuntu/.ssh/authorized_keys; "
            "fi; "
            "cat > /opt/rp/env; chmod 644 /opt/rp/env"
        )
    )
    try:
        result = ssh_runner(
            ssh_command(
                host=ssh_host,
                port=int(ssh_port) or 22,
                user=MGMT_SSH_USER,
                key_path=key_path,
                remote_command=remote_command,
            ),
            env_body + "\n",
        )
    except subprocess.TimeoutExpired as exc:
        raise BackendUnavailableError(f"retarget over SSH timed out: {exc}") from exc
    except OSError as exc:
        raise BackendUnavailableError(f"could not run ssh for retarget: {exc}") from exc
    if result.returncode != 0:
        raise BackendUnavailableError(
            f"retarget over SSH failed (exit {result.returncode}): {stderr_detail(result)}"
        )
    return True


def sandbox_tokens() -> dict[str, str]:
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        return {}
    tokens = {"HF_TOKEN": token}
    hub_token = os.environ.get("HUGGING_FACE_HUB_TOKEN", "")
    if hub_token:
        tokens["HUGGING_FACE_HUB_TOKEN"] = hub_token
    return tokens


def ssh_command(
    *, host: str, port: int, user: str, key_path: str, remote_command: str
) -> list[str]:
    return [
        "ssh",
        "-i", key_path,
        "-p", str(int(port) or 22),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={TRANSCRIPT_SSH_CONNECT_TIMEOUT}",
        f"{user}@{host}",
        remote_command,
    ]


def stderr_detail(result: subprocess.CompletedProcess[str]) -> str:
    lines = (result.stderr or "").strip().splitlines()
    return lines[-1] if lines else "no stderr"


def run_ssh(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, text=True, capture_output=True, timeout=TRANSCRIPT_READ_TIMEOUT_SECONDS
    )


def run_ssh_input(command: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=TRANSCRIPT_READ_TIMEOUT_SECONDS,
    )
