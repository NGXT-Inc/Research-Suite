"""Pure helpers, constants, and value types for the sandbox stack.

Everything here is free of ``SandboxService`` state — module-level functions,
tunables, and pure projection helpers.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from ..kernel.ports.sandbox_lifecycle import DEFAULT_STALE_PROVISION_DEADLINE_SECONDS
from ..kernel.utils import ValidationError, parse_iso as _parse_iso


VALID_GPUS: frozenset[str] = frozenset(
    {"T4", "L4", "A10G", "L40S", "A100", "A100-80GB", "H100", "B200"}
)
ACTIVE_SANDBOX_STATUSES: frozenset[str] = frozenset({"running"})
MAX_TIME_LIMIT_SECONDS = 24 * 60 * 60
MIN_TIME_LIMIT_SECONDS = 60
DEFAULT_TIME_LIMIT_SECONDS = 3600
DEFAULT_CPU = 2.0
DEFAULT_MEMORY_MB = 8192

# How long sandbox.request waits for a fresh provision to finish before it
# returns `provisioning` and tells the agent to poll. Kept safely under the MCP
# client timeout (~60s) so the call never trips it.
DEFAULT_REQUEST_WAIT_SECONDS = 45.0
# sandbox.runs long-poll: the server re-lists .runs receipts every POLL seconds
# and returns early on any terminal transition. The CAP is a server ceiling for
# clients with generous tool timeouts; clients at the common ~60s MCP floor
# (see DEFAULT_REQUEST_WAIT_SECONDS above) should pass wait_seconds<=45. The
# proxy stretches its HTTP timeout past the requested wait (proxy.py).
RUNS_WAIT_CAP_SECONDS = 300.0
RUNS_WAIT_POLL_SECONDS = 5.0
# Backstop: a `provisioning` row this old whose job is no longer in this process
# (daemon restart, or a wedged acquire) is reconciled to `failed`.
DEFAULT_STALE_PROVISION_SECONDS = 15 * 60.0
# Cadence hint handed to the agent while provisioning. Lambda VMs commonly
# take 5-15 minutes to boot and bootstrap, so a tighter cadence just burns
# calls without learning anything new.
POLL_AFTER_SECONDS = 30
# Live-usage samples are coalesced for this long so the fleet view and the
# drill-in terminal (which both poll ~3s) don't double-exec into a sandbox.
METRICS_CACHE_TTL_SECONDS = 2.0
# How often the reaper checks for sandboxes past their expires_at deadline and
# terminates them. Needed because Lambda VMs (unlike Modal sandboxes) have no
# server-side lifetime enforcement, so without this an expired VM bills forever.
DEFAULT_REAPER_INTERVAL_SECONDS = 30.0
DEFAULT_SANDBOX_IDLE_SECONDS = 3600.0

def _safe_name(identity: str) -> str:
    """Filesystem-safe key/conn filename for a sandbox identity."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in identity) or "sandbox"


# Markers the in-sandbox rec.sh ForceCommand wrapper writes to the transcript:
#   command start: "[<ts>] $ <command>"
#   command exit:  "[<ts>] (exit <code>)"   (rc captured via PIPESTATUS[0])
# Parsing them lets `terminal` report a structured exit status, so an agent can
# tell when a command finished and whether it succeeded instead of busy-polling
# the transcript tail. Best-effort: a sandbox created before the marker landed,
# an empty log, or a read taken mid-command simply yields None / False.
_EXIT_MARKER_RE = re.compile(r"^\[([^\]]*)\] \(exit (-?\d+)\)[ \t]*$", re.MULTILINE)
_CMD_MARKER_RE = re.compile(r"^\[([^\]]*)\] \$ (.*)$", re.MULTILINE)
COMMAND_OUTPUT_TAIL_CHARS = 2000


def parse_terminal_snapshot(transcript: str) -> dict[str, Any]:
    """Extract structured status for the latest command in a transcript."""
    empty = {
        "command_id": None,
        "command": "",
        "started_at": None,
        "status": "unknown",
        "exit_code": None,
        "finished_at": None,
        "output_tail": "",
    }
    if not transcript:
        return empty
    commands = list(_CMD_MARKER_RE.finditer(transcript))
    if not commands:
        return empty
    command = commands[-1]
    started_at = command.group(1).strip() or None
    command_text = command.group(2).strip()
    exits_after_command = [
        match for match in _EXIT_MARKER_RE.finditer(transcript, command.end())
    ]
    exit_match = exits_after_command[0] if exits_after_command else None
    if exit_match is None:
        output = transcript[command.end():]
        exit_code = None
        finished_at = None
        status = "running"
    else:
        output = transcript[command.end():exit_match.start()]
        exit_code = int(exit_match.group(2))
        finished_at = exit_match.group(1).strip() or None
        status = "succeeded" if exit_code == 0 else "failed"
    command_key = f"{len(commands)}\0{started_at or ''}\0{command_text}"
    command_id = "cmd_" + hashlib.sha1(command_key.encode("utf-8")).hexdigest()[:12]
    return {
        "command_id": command_id,
        "command": command_text,
        "started_at": started_at,
        "status": status,
        "exit_code": exit_code,
        "finished_at": finished_at,
        "output_tail": output[-COMMAND_OUTPUT_TAIL_CHARS:].lstrip("\n"),
    }


def parse_terminal_markers(transcript: str) -> tuple[int | None, str | None, bool]:
    """Extract ``(last_exit_code, last_command_finished_at, command_running)``.

    ``command_running`` is True when the most recent command-start marker has no
    following exit marker — i.e. a command is still in flight. A transcript with
    no markers (old sandbox, empty log) degrades to ``(None, None, False)``.
    """
    if not transcript:
        return None, None, False
    last_exit_code: int | None = None
    last_finished_at: str | None = None
    last_exit_end = -1
    exits = list(_EXIT_MARKER_RE.finditer(transcript))
    if exits:
        last = exits[-1]
        last_exit_code = int(last.group(2))
        last_finished_at = last.group(1).strip() or None
        last_exit_end = last.end()
    cmds = list(_CMD_MARKER_RE.finditer(transcript))
    command_running = bool(cmds and cmds[-1].start() > last_exit_end)
    return last_exit_code, last_finished_at, command_running


def validate_request_inputs(
    *,
    gpu: str | None,
    cpu: float | None,
    memory: int | None,
    time_limit: int | None,
    configurable_resources: bool = True,
) -> tuple[str | None, float, int, int]:
    norm_gpu: str | None = None
    if gpu not in (None, ""):
        norm_gpu = str(gpu).upper()
        # On configurable backends (Modal) `gpu` names a concrete attachable GPU,
        # so validate it against the supported set. On bundled-hardware backends
        # (Lambda Labs) `gpu` is only a free-form filter over live instance types
        # — the real selector is `instance_type` — so accept any string here and
        # let capacity resolution reject a genuinely unavailable choice.
        if configurable_resources and norm_gpu not in VALID_GPUS:
            raise ValidationError(
                f"invalid gpu: {gpu}; allowed: {', '.join(sorted(VALID_GPUS))}"
            )
    norm_cpu = float(cpu) if cpu is not None else DEFAULT_CPU
    if norm_cpu <= 0:
        raise ValidationError("cpu must be positive")
    norm_memory = int(memory) if memory is not None else DEFAULT_MEMORY_MB
    if norm_memory < 512:
        raise ValidationError("memory must be at least 512 (MiB)")
    norm_time = int(time_limit) if time_limit is not None else DEFAULT_TIME_LIMIT_SECONDS
    if norm_time < MIN_TIME_LIMIT_SECONDS or norm_time > MAX_TIME_LIMIT_SECONDS:
        raise ValidationError(
            f"time_limit must be between {MIN_TIME_LIMIT_SECONDS} and {MAX_TIME_LIMIT_SECONDS} seconds"
        )
    return norm_gpu, norm_cpu, norm_memory, norm_time


def parse_iso(value: Any) -> datetime | None:
    return _parse_iso(value)
