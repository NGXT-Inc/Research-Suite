"""Shared management-SSH operations for VM sandbox backends."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from backend.env import env_value
from backend.sandbox.sandbox_backend import BackendUnavailableError, TranscriptTail

from .sync_dirs import remote_experiment_dir, remote_root_of, remote_sessions_dir
from .transcript_wire import (
    TRANSCRIPT_TAIL_DEFAULT,
    parse_transcript_tail,
    transcript_tail_command,
)
from .run_receipts import parse_runs_listing, runs_listing_command
from .usage_metrics import METRICS_SCRIPT, parse_metrics
from .vm_bootstrap import (
    MGMT_SSH_USER,
    SESSIONS_DIR_NAME,
    TRANSCRIPT_FILENAME,
)


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
) -> TranscriptTail:
    if not sandbox_id or not ssh_host or not key_path:
        return TranscriptTail(data=b"", total_bytes=0)
    limit = int(tail) if tail and tail > 0 else TRANSCRIPT_TAIL_DEFAULT
    base = workdir or remote_experiment_dir(experiment_id=experiment_id, root=remote_root)
    log_path = PurePosixPath(
        remote_sessions_dir(experiment_id=experiment_id, root=remote_root_of(base)),
        TRANSCRIPT_FILENAME,
    ).as_posix()
    legacy_path = PurePosixPath(base, SESSIONS_DIR_NAME, experiment_id, TRANSCRIPT_FILENAME).as_posix()
    remote_command = transcript_tail_command(paths=[log_path, legacy_path], limit=limit)
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
    return parse_transcript_tail(result.stdout or "")


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


def read_runs_via_mgmt_ssh(
    *,
    ssh_runner: SshRunner,
    sandbox_id: str,
    workdir: str,
    ssh_host: str,
    ssh_port: int,
    key_path: str,
) -> list[dict[str, Any]] | None:
    """List merv_run receipts under workdir/.runs over the management channel.

    [] means "no runs"; None means unreachable/failed ("no news") so the
    observer never mistakes a dead channel for an empty runs dir.
    """
    if not sandbox_id or not ssh_host or not key_path or not workdir:
        return None
    try:
        result = ssh_runner(
            ssh_command(
                host=ssh_host,
                port=int(ssh_port) or 22,
                user=MGMT_SSH_USER,
                key_path=key_path,
                remote_command=runs_listing_command(experiment_dir=workdir),
            )
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    return parse_runs_listing(result.stdout or "")


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
        + shlex.quote("umask 077; cat > /opt/merv/secrets.env; chmod 600 /opt/merv/secrets.env")
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


def sandbox_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    token = os.environ.get("HF_TOKEN", "")
    if token:
        tokens["HF_TOKEN"] = token
        hub_token = os.environ.get("HUGGING_FACE_HUB_TOKEN", "")
        if hub_token:
            tokens["HUGGING_FACE_HUB_TOKEN"] = hub_token
    # MLflow credential pair (never the tracking URI — routing still flows
    # through mlflow.context): makes hosted-MLflow auth ambient in every SSH
    # session so agents never put the secret on a command line.
    agent_key = env_value("MERV_MLFLOW_AGENT_KEY") or ""
    if agent_key:
        tokens["MLFLOW_TRACKING_USERNAME"] = "rp-agent"
        tokens["MLFLOW_TRACKING_PASSWORD"] = agent_key
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
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=TRANSCRIPT_READ_TIMEOUT_SECONDS,
    )


def run_ssh_input(command: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=TRANSCRIPT_READ_TIMEOUT_SECONDS,
    )
