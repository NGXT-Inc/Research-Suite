"""Structured logs + per-tenant counters (cloud plan Phase 9).

Two cloud-operability primitives that are dormant in local mode and load-bearing
only in the control plane:

- ``StructuredLogger`` emits one redacted JSON line to stdout per tool call /
  HTTP request, carrying ``request_id`` + ``tenant_id`` + ``tool``/``path`` +
  ``status`` + ``duration_ms``. This is the cloud's structured log stream
  (scraped by the platform's log pipeline); it is SEPARATE from the daemon-local
  ``activity.jsonl`` (which never syncs). It is gated on control mode — local
  mode prints nothing new, so behavior is byte-identical. Redaction reuses the
  existing ``SENSITIVE_KEYS`` so a capability/token never reaches stdout.

- ``TenantCounters`` is the RED-ish per-tenant read: tool-call count (from the
  append-only ``events`` table, which the control plane already writes per
  project → tenant), sandbox-hours (the generation ledger, the same sum the
  spend accountant uses), and blob bytes (the per-tenant generation count is the
  cheap proxy; blob byte totals come from the store's blob accounting when the
  control plane carries it — kept conservative here). Reusing ``events`` for the
  audit trail (rather than a new audit table) is the deliberate lighter path:
  events are already tenant-scoped, append-only, and queried by the UI.

Audit-log placement (open decision J): cloud-only, via the existing ``events``
table scoped by project → tenant. No thin local mirror — the daemon keeps its
own ``activity.jsonl`` and that is the local record.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .config import resolve_auth_required
from .state.activity import SENSITIVE_KEYS


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop/redact any sensitive field before it reaches stdout."""
    return {
        key: ("[redacted]" if key in SENSITIVE_KEYS else value)
        for key, value in fields.items()
    }


class StructuredLogger:
    """Emits redacted JSON log lines to stdout, in control mode only.

    ``enabled`` defaults to "control mode is on" (``resolve_auth_required``) so
    local mode never gains stdout noise; tests force it on. ``stream`` is
    injectable so a test can capture without touching real stdout.
    """

    def __init__(self, *, enabled: bool | None = None, stream: Any | None = None) -> None:
        self.enabled = resolve_auth_required() if enabled is None else enabled
        self._stream = stream if stream is not None else sys.stdout

    def log(
        self,
        *,
        kind: str,
        request_id: str = "",
        tenant_id: str = "",
        tool: str = "",
        path: str = "",
        status: Any = "",
        duration_ms: int = 0,
        **extra: Any,
    ) -> None:
        if not self.enabled:
            return
        record = _redact(
            {
                "log": "rp.request",
                "kind": kind,
                "request_id": request_id,
                "tenant_id": tenant_id,
                "tool": tool,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
                **extra,
            }
        )
        # Drop empty optional fields so the line stays readable.
        record = {k: v for k, v in record.items() if v != "" or k == "status"}
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        print(line, file=self._stream, flush=True)


class TenantCounters:
    """RED-ish per-tenant counters for the control plane (cloud plan Phase 9)."""

    def __init__(self, *, store: Any) -> None:
        self.store = store

    def for_tenant(self, *, tenant_id: str) -> dict[str, Any]:
        """Tool calls, sandbox generations + hours, for one tenant.

        Tool calls are counted from the append-only ``events`` table joined
        through ``projects`` to the tenant — the control-plane audit trail. The
        ``sandbox_generations`` ledger gives generation count and accrued
        sandbox-hours (closed generations only here, so the number is stable;
        open-generation billing-to-now is the spend accountant's job).
        """
        conn = self.store.connect()
        try:
            tool_calls = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM events e
                JOIN projects p ON p.id = e.project_id
                WHERE p.tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
            gens = conn.execute(
                """
                SELECT price_usd_per_hour, started_at, ended_at
                FROM sandbox_generations WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchall()
        finally:
            conn.close()
        sandbox_hours = 0.0
        for row in gens:
            started = _parse(row["started_at"] if _has(row, "started_at") else None)
            ended = _parse(row["ended_at"] if _has(row, "ended_at") else None)
            if started is not None and ended is not None:
                sandbox_hours += max(0.0, (ended - started).total_seconds() / 3600.0)
        return {
            "tenant_id": tenant_id,
            "tool_calls": int(tool_calls["n"]) if tool_calls is not None else 0,
            "sandbox_generations": len(gens),
            "sandbox_hours": sandbox_hours,
        }


def _has(row: Any, key: str) -> bool:
    try:
        return key in row.keys()
    except AttributeError:
        return True


def _parse(value: Any):
    from datetime import UTC, datetime

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
