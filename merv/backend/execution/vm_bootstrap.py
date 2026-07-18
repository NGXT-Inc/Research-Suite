"""Shared bootstrap fragments for SSH-accessible VM sandbox backends."""

from __future__ import annotations

import base64
import shlex
from typing import Mapping

from .bootstrap_tools import REC_EXEC_CORE
from .run_receipts import RP_RUN_SCRIPT, rp_run_install_lines


SESSIONS_DIR_NAME = ".research_plugin_sessions"
TRANSCRIPT_FILENAME = "transcript.log"
MGMT_SSH_USER = "rpmgmt"


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
RP_SESSION_DIR="${RP_SESSION_DIR:-/workspace/.research_plugin_sessions/$RP_EXPERIMENT_ID}"
export RP_WORKDIR RP_EXPERIMENT_DIR RP_EXPERIMENT_ID RP_SANDBOX_DATA_DIR RP_DATASET_DIR RP_SESSION_DIR
mkdir -p "$RP_EXPERIMENT_DIR" "$RP_SANDBOX_DATA_DIR" "$RP_EXPERIMENT_DIR/artifacts_to_keep" "$RP_SESSION_DIR" 2>/dev/null || true
LOG_DIR="$RP_SESSION_DIR"
LOG="$LOG_DIR/transcript.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
if [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
  # File-transfer protocols (rsync/scp/sftp) speak a binary protocol over stdio.
  # The ForceCommand wrapper must hand them through untouched.
  case "$SSH_ORIGINAL_COMMAND" in
    rsync\ --server*|*"sftp-server"*|internal-sftp*|scp\ -*)
      exec bash -lc "$SSH_ORIGINAL_COMMAND"
      ;;
    rp-transcript-read:*)
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


MGMT_EXEC_SCRIPT = r"""#!/usr/bin/env bash
# research_plugin management channel (generated; plan Phase 5).
exec bash -lc "${SSH_ORIGINAL_COMMAND:-bash -l}"
"""


def build_bootstrap_core(
    *,
    public_key: str,
    experiment_id: str,
    workdir: str,
    sessions_dir: str,
    sandbox_data_dir: str,
    management_public_key: str = "",
    tokens: Mapping[str, str] | None = None,
    sshd_apply_command: str = "systemctl restart ssh || systemctl restart sshd || service ssh restart || true",
) -> str:
    """Phase 1 VM bootstrap: workspace, SSH keys, and rec.sh."""
    public_key_b64 = base64.b64encode(public_key.encode("utf-8")).decode("ascii")
    rec_script_b64 = base64.b64encode(REC_SCRIPT.encode("utf-8")).decode("ascii")
    rp_run_b64 = base64.b64encode(RP_RUN_SCRIPT.encode("utf-8")).decode("ascii")
    env_lines = build_runtime_env(
        experiment_id=experiment_id,
        workdir=workdir,
        sessions_dir=sessions_dir,
        sandbox_data_dir=sandbox_data_dir,
    )
    _ = tokens
    mgmt_block = ""
    if management_public_key:
        mgmt_key_b64 = base64.b64encode(
            management_public_key.encode("utf-8")
        ).decode("ascii")
        mgmt_exec_b64 = base64.b64encode(MGMT_EXEC_SCRIPT.encode("utf-8")).decode("ascii")
        mgmt_block = f"""
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
mkdir -p /opt/rp /root/.ssh /etc/ssh/sshd_config.d {shlex.quote(workdir)} {shlex.quote(sandbox_data_dir)} {shlex.quote(workdir)}/artifacts_to_keep {shlex.quote(sessions_dir)}
printf '%s' {shlex.quote(public_key_b64)} | base64 -d > /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
if id ubuntu >/dev/null 2>&1; then
  mkdir -p /home/ubuntu/.ssh
  printf '%s' {shlex.quote(public_key_b64)} | base64 -d >> /home/ubuntu/.ssh/authorized_keys
  chown -R ubuntu:ubuntu {shlex.quote(workdir)} {shlex.quote(sandbox_data_dir)} {shlex.quote(sessions_dir)}
  chown -R ubuntu:ubuntu /home/ubuntu/.ssh
  chmod 700 /home/ubuntu/.ssh
  chmod 600 /home/ubuntu/.ssh/authorized_keys
fi
cat > /opt/rp/env <<'RP_ENV'
{env_lines}
RP_ENV
printf '%s' {shlex.quote(rec_script_b64)} | base64 -d > /opt/rp/rec.sh
chmod +x /opt/rp/rec.sh
{rp_run_install_lines(script_b64=rp_run_b64)}{mgmt_block}cat > /etc/ssh/sshd_config.d/99-research-plugin.conf <<'RP_SSHD'
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile .ssh/authorized_keys
ForceCommand /opt/rp/rec.sh
PrintMotd no
AcceptEnv LANG LC_*
RP_SSHD
{sshd_apply_command}
"""

def build_standard_user_data(
    *,
    public_key: str,
    experiment_id: str,
    workdir: str,
    sessions_dir: str,
    sandbox_data_dir: str,
    management_public_key: str = "",
    apt_packages: tuple[str, ...] = (),
    python_packages: tuple[str, ...] = (),
) -> str:
    """Full two-phase user_data script for stock-Ubuntu VM providers.

    Phase 1 (bootstrap core) makes the VM reachable and recorded fast; phase 2
    installs the heavy toolchain the agents expect. Mirrors the Lambda Labs
    script; providers whose images pre-bundle ML tooling lose nothing — every
    phase-2 step is idempotent and tolerant of already-installed packages.
    """
    apt = " ".join(shlex.quote(pkg) for pkg in apt_packages)
    python = " ".join(shlex.quote(pkg) for pkg in python_packages)
    # mlflow gets --ignore-installed for images that ship Debian-owned Python
    # packages without RECORD files (pip cannot uninstall those).
    mlflow_package = shlex.quote("mlflow==2.18.0")
    bootstrap_core = build_bootstrap_core(
        public_key=public_key,
        experiment_id=experiment_id,
        workdir=workdir,
        sessions_dir=sessions_dir,
        sandbox_data_dir=sandbox_data_dir,
        management_public_key=management_public_key,
    )
    return f"""#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

{bootstrap_core}
# === Phase 2: heavy toolchain install (the VM is already usable by here) ===
apt-get update
apt-get install -y --no-install-recommends {apt}
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
install_with_uv_or_pip torch torchvision torchaudio || true
install_with_uv_or_pip {python} || true
"""


def build_runtime_env(
    *,
    experiment_id: str,
    workdir: str,
    sessions_dir: str,
    sandbox_data_dir: str,
) -> str:
    """Render /opt/rp/env for the experiment currently attached to the box."""
    return "\n".join(
        [
            f"RP_WORKDIR={shlex.quote(workdir)}",
            f"RP_EXPERIMENT_DIR={shlex.quote(workdir)}",
            f"RP_EXPERIMENT_ID={shlex.quote(experiment_id)}",
            f"RP_SANDBOX_DATA_DIR={shlex.quote(sandbox_data_dir)}",
            f"RP_DATASET_DIR={shlex.quote(sandbox_data_dir)}",
            f"RP_SESSION_DIR={shlex.quote(sessions_dir)}",
        ]
    )
