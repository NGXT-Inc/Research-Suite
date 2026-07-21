"""Hosted delivery logging, separate from durable operational queries."""

from __future__ import annotations

import json
import sys
from typing import Any

from .config import Mode, resolve_mode
from ..kernel.state.activity import redact_sensitive


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive fields before they reach stdout."""
    return redact_sensitive(value=fields)


class StructuredLogger:
    """Emits redacted JSON log lines to stdout, in control mode only.

    ``enabled`` defaults to "control mode is on" so
    local mode never gains stdout noise; tests force it on. ``stream`` is
    injectable so a test can capture without touching real stdout.
    """

    def __init__(self, *, enabled: bool | None = None, stream: Any | None = None) -> None:
        self.enabled = (resolve_mode() is Mode.CONTROL) if enabled is None else enabled
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
