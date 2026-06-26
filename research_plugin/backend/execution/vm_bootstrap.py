"""Shared bootstrap fragments for SSH-accessible VM sandbox backends."""

from __future__ import annotations

import base64
import shlex
from typing import Mapping

from .bootstrap_tools import REC_EXEC_CORE


SESSIONS_DIR_NAME = ".research_plugin_sessions"
TRANSCRIPT_FILENAME = "transcript.log"
MGMT_SSH_USER = "rpmgmt"
DASHBOARD_PORTS: Mapping[str, int] = {"tensorboard": 6006}
TRACKING_ENV_EXPORTS = (
    "MLFLOW_TRACKING_URI",
    "MLFLOW_EXPERIMENT_NAME",
    "RP_PROJECT_ID",
    "RP_ATTEMPT_ID",
    "RP_SANDBOX_ID",
    "RP_EXECUTION_BACKEND",
)


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
export RP_WORKDIR RP_EXPERIMENT_DIR RP_EXPERIMENT_ID RP_SANDBOX_DATA_DIR RP_DATASET_DIR RP_DASH_DIR RP_TB_LOGDIR
export MLFLOW_TRACKING_URI MLFLOW_EXPERIMENT_NAME RP_PROJECT_ID RP_ATTEMPT_ID RP_SANDBOX_ID RP_EXECUTION_BACKEND
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


DASHBOARD_SCRIPT = r"""#!/usr/bin/env bash
set +e
[ -f /opt/rp/env ] && . /opt/rp/env
RP_EXPERIMENT_ID="${RP_EXPERIMENT_ID:-unknown}"
RP_DASH_DIR="${RP_DASH_DIR:-/workspace/.research_plugin_sessions/$RP_EXPERIMENT_ID}"
RP_TB_LOGDIR="${RP_TB_LOGDIR:-$RP_DASH_DIR/tb}"
mkdir -p "$RP_TB_LOGDIR" 2>/dev/null || true

pid_alive() {
  pid_file="$1"
  [ -s "$pid_file" ] || return 1
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

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


def build_bootstrap_core(
    *,
    public_key: str,
    experiment_id: str,
    workdir: str,
    sessions_dir: str,
    sandbox_data_dir: str,
    management_public_key: str = "",
    tokens: Mapping[str, str] | None = None,
    tracking_env: Mapping[str, str] | None = None,
    sshd_apply_command: str = "systemctl restart ssh || systemctl restart sshd || service ssh restart || true",
) -> str:
    """Phase 1 VM bootstrap: workspace, SSH keys, rec.sh, and dashboards."""
    public_key_b64 = base64.b64encode(public_key.encode("utf-8")).decode("ascii")
    rec_script_b64 = base64.b64encode(REC_SCRIPT.encode("utf-8")).decode("ascii")
    dashboard_script_b64 = base64.b64encode(DASHBOARD_SCRIPT.encode("utf-8")).decode("ascii")
    env_lines = build_runtime_env(
        experiment_id=experiment_id,
        workdir=workdir,
        sessions_dir=sessions_dir,
        sandbox_data_dir=sandbox_data_dir,
        tracking_env=tracking_env,
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
printf '%s' {shlex.quote(dashboard_script_b64)} | base64 -d > /opt/rp/start_dashboards.sh
chmod +x /opt/rp/rec.sh
chmod +x /opt/rp/start_dashboards.sh
{mgmt_block}cat > /etc/ssh/sshd_config.d/99-research-plugin.conf <<'RP_SSHD'
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


def _tracking_env_lines(tracking_env: Mapping[str, str] | None) -> list[str]:
    lines: list[str] = []
    for key, value in sorted((tracking_env or {}).items()):
        key = str(key)
        if key in TRACKING_ENV_EXPORTS and value is not None:
            lines.append(f"{key}={shlex.quote(str(value))}")
    return lines


def build_runtime_env(
    *,
    experiment_id: str,
    workdir: str,
    sessions_dir: str,
    sandbox_data_dir: str,
    tracking_env: Mapping[str, str] | None = None,
) -> str:
    """Render /opt/rp/env for the experiment currently attached to the box."""
    return "\n".join(
        [
            f"RP_WORKDIR={shlex.quote(workdir)}",
            f"RP_EXPERIMENT_DIR={shlex.quote(workdir)}",
            f"RP_EXPERIMENT_ID={shlex.quote(experiment_id)}",
            f"RP_SANDBOX_DATA_DIR={shlex.quote(sandbox_data_dir)}",
            f"RP_DATASET_DIR={shlex.quote(sandbox_data_dir)}",
            f"RP_DASH_DIR={shlex.quote(sessions_dir)}",
            f"RP_TB_LOGDIR={shlex.quote(sessions_dir + '/tb')}",
            *_tracking_env_lines(tracking_env),
        ]
    )
