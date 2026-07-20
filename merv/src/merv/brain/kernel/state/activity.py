"""Append-only activity logging for HTTP and MCP visibility."""

from __future__ import annotations

from contextlib import suppress
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from merv.shared.project_dirs import (
    ensure_project_state_dir,
    resolve_project_state_dir,
)

from ..env import env_bool, env_value
from ..utils import now_iso


# Bound how much of the activity log recent() pulls into memory. On a
# long-lived daemon the JSONL file can reach gigabytes; reading the whole file
# per /api/activity poll (the UI polls it every few seconds) balloons RSS and
# stalls forwarded tool calls. We only ever need the tail, so cap the read to
# the last slice of the file.
TAIL_READ_BYTES = 16 * 1024 * 1024

# Cap the per-event result payload written to the log. Tool results such as
# experiment.get_state and the project home view can be many KB; logging them
# verbatim on every call — including frequent UI polls — is what drives
# multi-hundred-MB/day growth. The log is a visibility feed, not an archive.
RESULT_LOG_MAX_BYTES = 16 * 1024

SENSITIVE_KEYS = {
    "reviewer_capability",
    "capability",
    "MLFLOW_TRACKING_PASSWORD",
}
LOCAL_DATA_PLANE_KEYS = {"repo_root", "local_sync_dir", "local_experiment_dir"}
ID_KEYS = {
    "project_id",
    "claim_id",
    "experiment_id",
    "resource_id",
    "review_request_id",
    "review_session_id",
    "job_id",
    "target_type",
    "target_id",
    "role",
    "transition",
    "verdict",
}


class ActivityLogger:
    """Writes compact JSONL events without owning domain state."""

    def __init__(
        self,
        *,
        repo_root: Path,
        log_path: Path | None = None,
        enabled: bool | None = None,
        mirror_stderr: bool | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.enabled = env_bool("RESEARCH_PLUGIN_ACTIVITY_LOG", default=True) if enabled is None else enabled
        self.mirror_stderr = (
            env_bool("RESEARCH_PLUGIN_ACTIVITY_STDERR", default=False)
            if mirror_stderr is None
            else mirror_stderr
        )
        configured = env_value("MERV_ACTIVITY_LOG_PATH")
        explicit = log_path if log_path is not None else (Path(configured) if configured else None)
        if explicit is not None and not explicit.is_absolute():
            explicit = repo_root / explicit
        # Default path resolves per-write (see project_dirs: never cache).
        self._explicit_log_path = explicit

    @property
    def log_path(self) -> Path:
        if self._explicit_log_path is not None:
            return self._explicit_log_path
        return resolve_project_state_dir(self.repo_root) / "activity.jsonl"

    def emit(self, *, event_type: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        event = {
            "ts": now_iso(),
            "event": event_type,
            **payload,
        }
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
        if self._explicit_log_path is None:
            ensure_project_state_dir(self.repo_root)
        else:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if self.mirror_stderr:
            print(line, file=sys.stderr, flush=True)

    def tool_ok(
        self,
        *,
        source: str,
        tool: str,
        arguments: dict[str, Any],
        duration_ms: int,
        result: dict[str, Any],
    ) -> None:
        self.emit(
            event_type="tool.call",
            payload={
                "source": source,
                "tool": tool,
                "status": "ok",
                "duration_ms": duration_ms,
                "args": summarize_arguments(arguments=arguments),
                "result": cap_result(value=result),
                # Full I/O sizes in characters — what the agent actually sent and
                # received — independent of the capped `result`/summarized `args`
                # above. `received_chars` matches the MCP proxy's serialization
                # (json.dumps(result, sort_keys=True)) so it reflects the exact
                # payload that lands in the agent's context. This is the signal
                # the debug view sorts on to find context-bloating tools.
                "sent_chars": payload_chars(value=arguments),
                "received_chars": payload_chars(value=result),
            },
        )

    def tool_error(
        self,
        *,
        source: str,
        tool: str,
        arguments: dict[str, Any],
        duration_ms: int,
        error: str,
        error_code: str = "",
    ) -> None:
        self.emit(
            event_type="tool.call",
            payload={
                "source": source,
                "tool": tool,
                "status": "error",
                "duration_ms": duration_ms,
                "error": error,
                "error_code": error_code,
                "args": summarize_arguments(arguments=arguments),
                "sent_chars": payload_chars(value=arguments),
                "received_chars": len(error or ""),
            },
        )

    def http_request(self, *, method: str, path: str, status: int, duration_ms: int) -> None:
        self.emit(
            event_type="http.request",
            payload={
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
            },
        )

    def exception(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        exc: BaseException,
    ) -> None:
        """Log an exception event with full traceback.

        Both the HTTP server and the MCP server append to the same
        activity.jsonl, so this is the cross-process mechanism for getting
        a complete view of failures. The traceback string is included as a
        plain field so `tail -f`-ing the JSONL is enough to debug;
        `--activity-stderr` also mirrors the event to the running terminal.
        """
        tb_text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        self.emit(
            event_type=event_type,
            payload={
                **payload,
                "exc_type": type(exc).__name__,
                "exc_message": str(exc),
                "traceback": tb_text,
            },
        )

    def recent(
        self,
        *,
        limit: int = 100,
        source: str | None = None,
        event_filter: Callable[[dict[str, Any]], bool] | None = None,
        window: int = 5000,
    ) -> dict[str, Any]:
        """Return the most recent activity events.

        The `limit` cap is applied AFTER source filtering so a request for
        "last 300 MCP events" returns 300 MCP events (rather than 300 events
        of any source from which MCP is a fraction).

        `window` bounds how far back we scan for the source filter and for
        the summary counts. It is also a backstop against pathological log
        growth on long-lived servers.
        """
        limit = max(1, min(limit, 1000))
        window = max(limit, min(window, 50000))
        empty_summary = {"total": 0, "source_counts": {}, "event_counts": {}, "status_counts": {"ok": 0, "error": 0}}
        if not self.log_path.exists():
            return {"events": [], "summary": empty_summary}
        raw = self._tail_lines(max_lines=window, max_bytes=TAIL_READ_BYTES)
        scanned: list[dict[str, Any]] = []
        for line in raw:
            try:
                scanned.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        source_counts: dict[str, int] = {}
        event_counts: dict[str, int] = {}
        status_counts = {"ok": 0, "error": 0}
        for event in scanned:
            eff = effective_source(event=event)
            source_counts[eff] = source_counts.get(eff, 0) + 1
            etype = event.get("event") or "unknown"
            event_counts[etype] = event_counts.get(etype, 0) + 1
            if is_event_ok(event=event):
                status_counts["ok"] += 1
            else:
                status_counts["error"] += 1
        if source is not None:
            filtered = [e for e in scanned if effective_source(event=e) == source]
        else:
            filtered = scanned
        if event_filter is not None:
            filtered = [e for e in filtered if event_filter(e)]
        return {
            "events": filtered[-limit:],
            # The full post-filter, pre-limit list over the scan window. Callers
            # that need a scope-correct summary (the tenant-scoped HTTP path)
            # summarize over this rather than the trimmed `events` slice, while
            # the project-blind `summary` below stays usable only where no filter
            # is applied (single-project local mode).
            "scanned_filtered": filtered,
            "summary": {
                "total": len(scanned),
                "source_counts": source_counts,
                "event_counts": event_counts,
                "status_counts": status_counts,
                "window": len(scanned),
            },
        }

    def _tail_lines(self, *, max_lines: int, max_bytes: int) -> list[str]:
        """Return up to the last `max_lines` complete lines from the log.

        Reads at most `max_bytes` from the end of the file, so memory stays
        bounded no matter how large the log has grown. If the byte budget lands
        mid-line, the first (partial) line is dropped.
        """
        with self.log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            read_size = min(size, max_bytes)
            handle.seek(size - read_size)
            chunk = handle.read(read_size)
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        if read_size < size and lines:
            lines = lines[1:]
        return lines[-max_lines:]


def effective_source(*, event: dict[str, Any]) -> str:
    """Treat http.request events as having an implicit source = http."""
    if event.get("event") == "http.request":
        return "http"
    return event.get("source") or "mcp"


def is_event_ok(*, event: dict[str, Any]) -> bool:
    if event.get("event") == "http.request":
        status = event.get("status")
        return not (isinstance(status, int) and status >= 400)
    status = event.get("status")
    return status in (None, "ok")


def summarize_arguments(*, arguments: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in arguments.items():
        if key in SENSITIVE_KEYS:
            summary[key] = "[redacted]"
        elif key in ID_KEYS:
            summary[key] = value
    return summary


def payload_chars(*, value: Any) -> int:
    """Length (in chars) of a value serialized the way the agent sees it.

    Matches the MCP proxy's `json.dumps(result, sort_keys=True)` so the count is
    the true size of the JSON text that enters the agent's context. Returns 0 on
    any serialization failure rather than raising — this is telemetry.
    """
    try:
        return len(json.dumps(jsonable(value=value), sort_keys=True))
    except (TypeError, ValueError):
        return 0


def cap_result(*, value: Any) -> Any:
    """Return a JSON-safe result capped to RESULT_LOG_MAX_BYTES.

    Oversized results are replaced with a compact truncation marker so the
    activity log stays bounded. The caller still received the full result; the
    log is a visibility feed, not an archive.
    """
    safe = redact_sensitive(value=jsonable(value=value))
    try:
        encoded = json.dumps(safe, separators=(",", ":"))
    except (TypeError, ValueError):
        return safe
    if len(encoded) <= RESULT_LOG_MAX_BYTES:
        return safe
    return {
        "_truncated": True,
        "_bytes": len(encoded),
        "preview": encoded[:2048],
    }


def jsonable(*, value: Any) -> Any:
    with suppress(TypeError, ValueError):
        json.dumps(value)
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(value=item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(value=item) for item in value]
    return str(value)


def redact_sensitive(*, value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if key in SENSITIVE_KEYS
            else redact_sensitive(value=item)
            for key, item in value.items()
            if key not in LOCAL_DATA_PLANE_KEYS
        }
    if isinstance(value, list):
        return [redact_sensitive(value=item) for item in value]
    return value


def monotonic_ms() -> int:
    return int(time.perf_counter() * 1000)
