"""merv_run launch convention: detached runs with file receipts.

The sandbox-side contract is files, not services: `merv_run <label> -- <cmd>`
detaches the command under ``$MERV_EXPERIMENT_DIR/.runs/<label>/`` and the
WRAPPER (not the command) writes ``finished_at`` then ``exit_code`` when the
command exits — so the sentinel survives SSH disconnects, and only box death
loses it. The brain observes runs by listing that directory over the same
management channel used for transcripts/metrics; no daemon, no registration
call, no provider API.
"""

from __future__ import annotations

import json
import shlex
from typing import Any


RUNS_DIR_NAME = ".runs"
MERV_RUN_PATH = "/opt/merv/merv_run"

# Installed on every sandbox next to rec.sh and symlinked onto PATH.
MERV_RUN_SCRIPT = r"""#!/bin/sh
# merv_run <label> -- <command> [args...]: launch a long command detached, with
# receipts under $MERV_EXPERIMENT_DIR/.runs/<label>/ (meta.json, log.txt, and —
# written by this wrapper when the command exits — finished_at + exit_code).
# The exit_code file is the completion sentinel: it survives SSH disconnects.
set -u
usage() { echo 'usage: merv_run <label> -- <command> [args...]' >&2; exit 2; }
[ $# -ge 3 ] || usage
label=$1; shift
[ "$1" = '--' ] || usage
shift
case $label in
  *[!A-Za-z0-9._-]*|'') echo "merv_run: label must be non-empty [A-Za-z0-9._-]" >&2; exit 2 ;;
esac
runs=${MERV_EXPERIMENT_DIR:?merv_run: MERV_EXPERIMENT_DIR is not set}/.runs
dir=$runs/$label
# mkdir without -p is the duplicate-label guard: refuse rather than suffix so
# labels stay stable for the observer.
if ! mkdir -p "$runs" || ! mkdir "$dir" 2>/dev/null; then
  echo "merv_run: run '$label' already exists in $runs — pick a new label" >&2
  exit 2
fi
# tr first: a raw newline/CR/tab inside an argument would break the one-line
# JSON receipt below (and sed is line-oriented, so it must never see them).
esc() { printf '%s' "$1" | tr '\n\r\t' '   ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
# The watcher (not the command) writes the receipts: finished_at first, then
# exit_code — the sentinel is last so its presence implies a complete record.
WATCH='dir=$1; shift
"$@" >"$dir/log.txt" 2>&1
rc=$?
date -u +%Y-%m-%dT%H:%M:%SZ >"$dir/finished_at"
echo "$rc" >"$dir/exit_code"'
# setsid detaches from the SSH session so a disconnect cannot signal the run;
# hosts without setsid (macOS test runs) still detach via nohup + background.
if command -v setsid >/dev/null 2>&1; then
  setsid nohup sh -c "$WATCH" merv_run_watch "$dir" "$@" </dev/null >/dev/null 2>&1 &
else
  nohup sh -c "$WATCH" merv_run_watch "$dir" "$@" </dev/null >/dev/null 2>&1 &
fi
pid=$!
printf '{"label":"%s","command":"%s","pid":%d,"started_at":"%s"}\n' \
  "$(esc "$label")" "$(esc "$*")" "$pid" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$dir/meta.json"
echo "merv_run: started '$label' (pid $pid) — log: $dir/log.txt (sentinel: $dir/exit_code)"
"""


def runs_listing_command(*, experiment_dir: str) -> str:
    """One-shot remote listing of every run's receipts (no log bytes).

    Emits one ``===MERV_RUN <label>`` block per run dir with the meta.json body
    and the sentinel files. A missing .runs dir exits 0 with no output — the
    observer treats that as "no runs" at the cost of one cheap ssh exec.
    """
    runs_dir = f"{experiment_dir.rstrip('/')}/{RUNS_DIR_NAME}"
    return (
        f"d={shlex.quote(runs_dir)}; [ -d \"$d\" ] || exit 0; "
        "for r in \"$d\"/*/; do [ -d \"$r\" ] || continue; "
        "printf '===MERV_RUN %s\\n' \"$(basename \"$r\")\"; "
        "cat \"$r/meta.json\" 2>/dev/null; printf '\\n'; "
        "printf '===EXIT %s\\n' \"$(cat \"$r/exit_code\" 2>/dev/null)\"; "
        "printf '===FIN %s\\n' \"$(cat \"$r/finished_at\" 2>/dev/null)\"; "
        "done"
    )


def parse_runs_listing(output: str) -> list[dict[str, Any]]:
    """Parse `runs_listing_command` stdout into run records.

    Each record: label, command, pid, started_at, exit_code (int | None) and
    finished_at (str, '' while running). Unparseable meta.json degrades to
    empty fields — the label and the sentinel are the load-bearing facts.
    """
    runs: list[dict[str, Any]] = []
    for block in output.split("===MERV_RUN "):
        block = block.strip()
        if not block:
            continue
        label, _, rest = block.partition("\n")
        meta: dict[str, Any] = {}
        exit_code: int | None = None
        finished_at = ""
        for line in rest.splitlines():
            if line.startswith("===EXIT "):
                raw = line[len("===EXIT "):].strip()
                try:
                    exit_code = int(raw)
                except ValueError:
                    exit_code = None
            elif line.startswith("===FIN "):
                finished_at = line[len("===FIN "):].strip()
            elif line.startswith("{") and not meta:
                try:
                    parsed = json.loads(line)
                    meta = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    meta = {}
        runs.append(
            {
                "label": label.strip(),
                "command": str(meta.get("command") or ""),
                "pid": meta.get("pid"),
                "started_at": str(meta.get("started_at") or ""),
                "exit_code": exit_code,
                "finished_at": finished_at,
            }
        )
    return runs


def merv_run_install_lines(*, script_b64: str) -> str:
    """Bootstrap fragment installing merv_run beside rec.sh and onto PATH.

    Also links the legacy ``rp_run`` name as a one-version compat shim for
    agents still typing the old command; remove next release.
    """
    return (
        f"printf '%s' {shlex.quote(script_b64)} | base64 -d > {MERV_RUN_PATH}\n"
        f"chmod +x {MERV_RUN_PATH}\n"
        f"ln -sf {MERV_RUN_PATH} /usr/local/bin/merv_run\n"
        f"ln -sf {MERV_RUN_PATH} /usr/local/bin/rp_run\n"
    )
