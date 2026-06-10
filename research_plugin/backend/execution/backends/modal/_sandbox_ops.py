"""Small helpers for interacting with Modal sandboxes."""

from __future__ import annotations

import asyncio
import inspect
import shlex
import time
from typing import Any

from ...errors import BackendUnavailableError


# Retryable errors while Modal releases a prior sandbox filesystem operation.
TRANSIENT_VOLUME_ERRORS: tuple[str, ...] = (
    "Operation not permitted",
    "operation not permitted",
    "Resource busy",
    "Device or resource busy",
    "Transport endpoint is not connected",
)


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


def exec_checked(
    *,
    sandbox: Any,
    command: str,
    timeout: int,
    retries: int = 0,
    retry_on: tuple[str, ...] = (),
    retry_backoff_seconds: float = 4.0,
) -> None:
    """Run a bash command in the sandbox, raising on non-zero exit."""
    attempt = 0
    while True:
        process = sandbox.exec("bash", "-c", command, timeout=timeout)
        exit_code = wait_process(process)
        if exit_code == 0:
            return
        stderr = read_stream(getattr(process, "stderr", None))
        if attempt < retries and any(marker in stderr for marker in retry_on):
            attempt += 1
            time.sleep(retry_backoff_seconds * attempt)
            continue
        raise BackendUnavailableError(
            f"Modal sandbox command failed with exit code {exit_code}: {stderr}"
        )


def ensure_remote_dir(*, sandbox: Any, path: str) -> None:
    # Control-plane mkdir may fail transiently; shell mkdir is more tolerant.
    filesystem_mkdir = getattr(getattr(sandbox, "filesystem", None), "make_directory", None)
    if callable(filesystem_mkdir):
        try:
            maybe_await(filesystem_mkdir(path, create_parents=True))
            return
        except Exception:  # noqa: BLE001
            pass
    mkdir = getattr(sandbox, "mkdir", None)
    if callable(mkdir):
        try:
            maybe_await(mkdir(path, parents=True))
            return
        except Exception:  # noqa: BLE001
            pass
    exec_checked(sandbox=sandbox, command=f"mkdir -p {shlex.quote(path)}", timeout=60)
