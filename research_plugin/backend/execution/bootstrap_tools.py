"""Shared tool baselines for SSH-accessible compute environments."""

from __future__ import annotations


BASELINE_APT_PACKAGES: tuple[str, ...] = (
    "bash",
    "sudo",
    "git",
    "rsync",
    "ripgrep",
    "fd-find",
    "findutils",
    "grep",
    "sed",
    "gawk",
    "jq",
    "tree",
    "less",
    "file",
    "curl",
    "wget",
    "tar",
    "gzip",
    "zip",
    "unzip",
    "xz-utils",
    "python3",
    "python3-venv",
    "python3-pip",
    "build-essential",
    "pkg-config",
    "cmake",
    "ninja-build",
    "git-lfs",
    "procps",
    "util-linux",
    "iproute2",
    "dnsutils",
    "lsof",
    "tmux",
)


# Shared command-execution core for the sshd ForceCommand wrapper (rec.sh).
#
# Runs every non-bypassed SSH command inside a detached tmux session so the
# command's lifetime is anchored to the VM, not to the SSH channel: a dropped
# connection or a timed-out agent tool call stops the *viewing*, never the
# *work*. The foreground side streams the output file back byte-for-byte and
# returns the real exit code, so short commands behave exactly like a plain
# `ssh host 'cmd'`.
#
# Contract kept with the daemon's transcript parser (sandbox.terminal):
#   [<ts>] $ <command>   - written by the foreground wrapper at start
#   [<ts>] (exit <rc>)   - written by the tmux side when the command ends,
#                          so it lands even if nobody is connected.
#
# Expects the surrounding script to have set: $LOG, $RP_WORKDIR,
# $RP_SANDBOX_DATA_DIR, a ts() helper, and $SSH_ORIGINAL_COMMAND. Every path
# ends in `exit`. If tmux is unavailable or fails to start, falls back open to
# the legacy attached execution (work then dies with the channel, as before).
#
# The command's stdin is /dev/null: agents pass input as in-command heredocs
# (`python3 - <<'PY' ... PY`), which the remote bash evaluates from the
# command string itself; rsync/scp/sftp bypass this core entirely.
REC_EXEC_CORE = r"""rp_exec_attached() {
  bash -lc "$SSH_ORIGINAL_COMMAND" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  { printf '[%s] (exit %d)\n' "$(ts)" "$rc" >> "$LOG"; } 2>/dev/null || true
  exit "$rc"
}
rp_drain() {
  size=$(wc -c < "$RUN_DIR/out" 2>/dev/null || echo 0)
  size=${size//[^0-9]/}
  size=${size:-0}
  if [ "$size" -gt "$RP_OFFSET" ]; then
    tail -c +"$((RP_OFFSET + 1))" "$RUN_DIR/out" | head -c "$((size - RP_OFFSET))"
    RP_OFFSET=$size
  fi
}
command -v tmux >/dev/null 2>&1 || rp_exec_attached
RUNS_DIR="${RP_SANDBOX_DATA_DIR:-/workspace/data}/.rp_runs"
RUN_ID="rp_$(date +%s)_$$"
RUN_DIR="$RUNS_DIR/$RUN_ID"
mkdir -p "$RUN_DIR" 2>/dev/null || rp_exec_attached
printf '%s' "$SSH_ORIGINAL_COMMAND" > "$RUN_DIR/cmd"
export -p > "$RUN_DIR/env" 2>/dev/null || : > "$RUN_DIR/env"
: > "$RUN_DIR/out"
{
  printf '#!/usr/bin/env bash\n'
  printf '. "%s/env" 2>/dev/null || true\n' "$RUN_DIR"
  printf 'cd "%s" 2>/dev/null || true\n' "$RP_WORKDIR"
  printf 'bash -lc "$(cat "%s/cmd")" < /dev/null 2>&1 | tee -a "%s" >> "%s/out"\n' "$RUN_DIR" "$LOG" "$RUN_DIR"
  printf 'rc=${PIPESTATUS[0]}\n'
  printf 'printf "%%s" "$rc" > "%s/exit_code.tmp"\n' "$RUN_DIR"
  printf 'mv "%s/exit_code.tmp" "%s/exit_code"\n' "$RUN_DIR" "$RUN_DIR"
  printf '{ printf "[%%s] (exit %%d)\\n" "$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)" "$rc" >> "%s"; } 2>/dev/null || true\n' "$LOG"
} > "$RUN_DIR/run.sh"
chmod +x "$RUN_DIR/run.sh" 2>/dev/null || true
tmux new-session -d -s "$RUN_ID" "bash '$RUN_DIR/run.sh'" 2>/dev/null || rp_exec_attached
printf '[rp] %s under tmux supervisor: survives SSH disconnect; if this call times out the command is STILL RUNNING - check the transcript before re-running.\n' "$RUN_ID" >&2
RP_OFFSET=0
while :; do
  rp_drain
  if [ -f "$RUN_DIR/exit_code" ]; then
    rp_drain
    exit "$(cat "$RUN_DIR/exit_code")"
  fi
  if ! tmux has-session -t "$RUN_ID" 2>/dev/null; then
    if [ -f "$RUN_DIR/exit_code" ]; then
      rp_drain
      exit "$(cat "$RUN_DIR/exit_code")"
    fi
    rp_drain
    printf '[rp] %s: tmux session ended without an exit code (killed or OOM?)\n' "$RUN_ID" >&2
    { printf '[%s] (exit %d)\n' "$(ts)" 137 >> "$LOG"; } 2>/dev/null || true
    exit 137
  fi
  sleep 0.2
done"""


MODAL_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)


LAMBDA_APT_PACKAGES: tuple[str, ...] = (
    "openssh-server",
    "ca-certificates",
    *BASELINE_APT_PACKAGES,
)


ML_PYTHON_PACKAGES: tuple[str, ...] = (
    "transformers",
    "numpy",
    "matplotlib",
    "pandas",
    "scikit-learn",
)
