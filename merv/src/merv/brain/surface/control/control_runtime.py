"""Control-plane runtime adapters with no local workspace dependencies."""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ...kernel.state.tool_call_stats import by_tool, tool_call_totals
from ...kernel.state.activity import (
    cap_result,
    effective_source,
    is_event_ok,
    payload_chars,
    redact_sensitive,
    summarize_arguments,
)
from ...kernel.utils import ValidationError, now_iso, parse_iso


class ControlActivitySink:
    """Bounded in-memory activity sink for the unified brain composition."""

    log_path = "<control-activity-disabled>"

    def __init__(self, *, max_events: int = 5000) -> None:
        self.max_events = max_events
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def emit(self, *, event_type: str, payload: dict[str, Any]) -> None:
        event = {"ts": now_iso(), "event": event_type, **payload}
        with self._lock:
            self._events.append(event)
            del self._events[: max(0, len(self._events) - self.max_events)]

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

    def recent(
        self,
        *,
        limit: int = 100,
        source: str | None = None,
        event_filter: Any | None = None,
        window: int = 5000,
    ) -> dict[str, Any]:
        with self._lock:
            scanned = list(self._events[-max(1, min(window, self.max_events)):])
        summary = _activity_summary(scanned)
        if source is not None:
            scanned = [
                event for event in scanned if effective_source(event=event) == source
            ]
        if event_filter is not None:
            scanned = [event for event in scanned if event_filter(event)]
        limit = max(1, min(limit, 1000))
        return {
            "events": scanned[-limit:],
            "scanned_filtered": scanned,
            "summary": summary,
        }


class ControlToolCallSink:
    """Bounded in-memory tool-call sink for the unified brain composition."""

    db_path = "<control-tool-calls-disabled>"

    def __init__(self, *, max_rows: int = 1500) -> None:
        self.max_rows = max_rows
        self._next_id = 1
        self._calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(
        self,
        *,
        tool: str,
        source: str,
        status: str,
        duration_ms: int,
        arguments: dict[str, Any],
        result: Any | None = None,
        error: str = "",
        error_code: str = "",
    ) -> None:
        target_type, target_id = _target_of(arguments)
        row = {
            "id": self._next_id,
            "ts": now_iso(),
            "tool": tool,
            "source": source,
            "status": status,
            "duration_ms": int(duration_ms or 0),
            "sent_chars": payload_chars(value=arguments),
            "received_chars": len(error or "")
            if status == "error"
            else payload_chars(value=result),
            "error_code": error_code,
            "project_id": str(arguments.get("project_id") or ""),
            "target_type": target_type,
            "target_id": target_id,
            "args": redact_sensitive(value=dict(arguments)),
            "result": error if status == "error" else redact_sensitive(value=result),
            "args_truncated": False,
            "result_truncated": False,
        }
        with self._lock:
            self._next_id += 1
            self._calls.append(row)
            del self._calls[: max(0, len(self._calls) - self.max_rows)]

    def stats(
        self,
        *,
        minutes: int | None = None,
        source: str | None = None,
        status: str | None = None,
        tool: str | None = None,
        project_id: str | None = None,
        project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
        limit: int = 200,
        sort: str = "ts",
        order: str = "desc",
    ) -> dict[str, Any]:
        with self._lock:
            calls = [dict(row) for row in self._calls]
        calls = [
            row
            for row in calls
            if _tool_call_matches(
                row,
                minutes=minutes,
                source=source,
                status=status,
                tool=tool,
                project_id=project_id,
                project_ids=project_ids,
            )
        ]
        totals = tool_call_totals(calls)
        sortable = {"ts", "received_chars", "sent_chars", "duration_ms", "tool"}
        sort = sort if sort in sortable else "ts"
        calls.sort(
            key=lambda row: row.get(sort) if row.get(sort) is not None else 0,
            reverse=(str(order).lower() != "asc"),
        )
        limit = max(1, min(int(limit), 2000))
        visible = [_tool_call_summary(row) for row in calls[:limit]]
        return {
            "calls": visible,
            "by_tool": by_tool(calls),
            "totals": totals,
            "coverage": {
                "calls": totals["calls"],
                "stored": len(calls),
                "oldest_ts": min((row["ts"] for row in calls), default=None),
                "newest_ts": max((row["ts"] for row in calls), default=None),
                "capped": False,
            },
            "filter": {
                "minutes": minutes,
                "source": source,
                "status": status,
                "tool": tool,
                "project_id": project_id,
            },
        }

    def get(
        self, *, call_id: int, project_ids: set[str] | None = None
    ) -> dict[str, Any] | None:
        allowed = {str(pid) for pid in project_ids or [] if str(pid)}
        with self._lock:
            for row in self._calls:
                if int(row["id"]) != int(call_id):
                    continue
                if (
                    project_ids is not None
                    and str(row.get("project_id") or "") not in allowed
                ):
                    return None
                return dict(row)
        return None

    def clear(self, *, project_ids: set[str] | None = None) -> dict[str, Any]:
        allowed = {str(pid) for pid in project_ids or [] if str(pid)}
        with self._lock:
            if project_ids is None:
                cleared = len(self._calls)
                self._calls.clear()
                return {"cleared": cleared}
            before = len(self._calls)
            self._calls = [
                row
                for row in self._calls
                if str(row.get("project_id") or "") not in allowed
            ]
            return {"cleared": before - len(self._calls)}


class ControlSandboxWorker:
    """Brain-side adapter for machine-local sandbox enrichment.

    The brain never owns caller-side keys or connection files, so the worker
    exposes neutral empty enrichment while provider endpoint refresh remains a
    control-plane operation.
    """

    def repo_relative(self, path: str | Path) -> str:
        return str(path)

    def local_experiment_dir(self, **_: Any) -> Path:
        return Path("")

    def ensure_keypair(self, **_: Any) -> tuple[str, Path]:
        raise ValidationError(
            "control mode cannot mint data-plane user SSH keys; pass public_key "
            "through the local MCP proxy"
        )

    def sandbox_enrichment(self, **_: Any) -> dict[str, Any]:
        return {}


class ControlTaskChannel:
    """Neutral brain task channel for sandbox lifecycle callbacks.

    The brain updates sandbox rows and reapers through the neutral SandboxBackend
    protocol; caller-machine conn-file work lives in the local MCP proxy.
    """

    def __init__(self) -> None:
        self.history: list[tuple[str, dict[str, Any]]] = []

    def submit(
        self,
        *,
        task_type: str,
        payload: dict[str, Any],
        deadline: str | None = None,
        tenant_id: str | None = None,
    ) -> Any:
        del deadline, tenant_id
        self.history.append((task_type, deepcopy(payload)))
        if task_type == "conn_refresh":
            return {}
        if task_type == "teardown":
            return None
        raise ValidationError(f"unknown task type: {task_type}")


def _activity_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "total": len(events),
        "source_counts": {},
        "event_counts": {},
        "status_counts": {"ok": 0, "error": 0},
        "window": len(events),
    }
    for event in events:
        source = effective_source(event=event)
        event_type = str(event.get("event") or "unknown")
        summary["source_counts"][source] = summary["source_counts"].get(source, 0) + 1
        summary["event_counts"][event_type] = (
            summary["event_counts"].get(event_type, 0) + 1
        )
        status = "ok" if is_event_ok(event=event) else "error"
        summary["status_counts"][status] += 1
    return summary


def _target_of(arguments: Any) -> tuple[str | None, str | None]:
    if not isinstance(arguments, dict):
        return None, None
    for target_type, key in (
        ("experiment", "experiment_id"),
        ("claim", "claim_id"),
        ("resource", "resource_id"),
    ):
        if arguments.get(key):
            return target_type, str(arguments[key])
    review = arguments.get("review_id") or arguments.get("request_id")
    return ("review", str(review)) if review else (None, None)


def _tool_call_matches(
    row: dict[str, Any],
    *,
    minutes: int | None,
    source: str | None,
    status: str | None,
    tool: str | None,
    project_id: str | None,
    project_ids: set[str] | list[str] | tuple[str, ...] | None,
) -> bool:
    if minutes and minutes > 0:
        cutoff = datetime.now(tz=UTC) - timedelta(minutes=minutes)
        ts = parse_iso(row.get("ts"))
        if ts is None or ts < cutoff:
            return False
    if source and source != "all" and row.get("source") != source:
        return False
    if status and status != "all" and row.get("status") != status:
        return False
    if tool and tool not in str(row.get("tool") or ""):
        return False
    if project_id and row.get("project_id") != project_id:
        return False
    if project_ids is not None:
        allowed = {str(pid) for pid in project_ids if str(pid)}
        return bool(allowed) and str(row.get("project_id") or "") in allowed
    return True


def _tool_call_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id",
            "ts",
            "tool",
            "source",
            "status",
            "duration_ms",
            "sent_chars",
            "received_chars",
            "error_code",
            "project_id",
            "target_type",
            "target_id",
        )
    }
