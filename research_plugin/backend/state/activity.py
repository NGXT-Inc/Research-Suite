"""Append-only activity logging for HTTP and MCP visibility."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from ..utils import now_iso


SENSITIVE_KEYS = {"reviewer_capability", "capability"}
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


def env_flag(*, name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "off", "no"}


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
        self.enabled = env_flag(name="RESEARCH_PLUGIN_ACTIVITY_LOG", default=True) if enabled is None else enabled
        self.mirror_stderr = (
            env_flag(name="RESEARCH_PLUGIN_ACTIVITY_STDERR", default=False)
            if mirror_stderr is None
            else mirror_stderr
        )
        configured = os.environ.get("RESEARCH_PLUGIN_ACTIVITY_LOG_PATH")
        if log_path is not None:
            self.log_path = log_path
        elif configured:
            self.log_path = Path(configured)
        else:
            self.log_path = repo_root / ".research_plugin" / "activity.jsonl"
        if not self.log_path.is_absolute():
            self.log_path = repo_root / self.log_path

    def emit(self, *, event_type: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        event = {
            "ts": now_iso(),
            "event": event_type,
            **payload,
        }
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
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
                "result": jsonable(value=result),
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
        raw = self.log_path.read_text(encoding="utf-8").splitlines()[-window:]
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
        return {
            "events": filtered[-limit:],
            "summary": {
                "total": len(scanned),
                "source_counts": source_counts,
                "event_counts": event_counts,
                "status_counts": status_counts,
                "window": len(scanned),
            },
        }


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


def jsonable(*, value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        return {str(key): jsonable(value=item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(value=item) for item in value]
    return str(value)


def monotonic_ms() -> int:
    return int(time.perf_counter() * 1000)
