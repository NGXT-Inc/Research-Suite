"""Small helpers for interacting with Modal sandboxes."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any


def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def wait_process(process: Any) -> int:
    wait = getattr(process, "wait", None)
    if callable(wait):
        result = wait()
        return int(result or 0)
    return int(getattr(process, "returncode", 0) or 0)


def read_stream(stream: Any) -> str:
    if stream is None:
        return ""
    read = getattr(stream, "read", None)
    if not callable(read):
        return ""
    raw = read() or ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)
