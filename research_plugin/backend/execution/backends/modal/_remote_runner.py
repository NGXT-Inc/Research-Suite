#!/usr/bin/env python3
"""Remote process supervisor copied into each Modal sandbox job directory."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
import traceback
from datetime import datetime, timezone


POLL_SECONDS = 0.25
TERM_GRACE_SECONDS = 2.0
# Periodically flush status/logs so mid-run sandbox loss still leaves state.
COMMIT_INTERVAL_SECONDS = 30.0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def commit_volume(volume_mount: str | None, volume_name: str | None = None) -> str | None:
    """Flush OS buffers for Modal's background Volume commits."""
    if not volume_mount:
        return None
    try:
        process = subprocess.run(
            ["sync", volume_mount],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if process.returncode != 0:
            stderr = (process.stderr or process.stdout or "").strip()
            return f"sync exited with code {process.returncode}: {stderr}"
    except Exception as exc:  # noqa: BLE001
        return f"sync {type(exc).__name__}: {exc}"
    return None


def finalize(args: argparse.Namespace, status: dict) -> None:
    write_status(args.status, status)
    commit_error = commit_volume(
        getattr(args, "volume_mount", None),
        getattr(args, "volume_name", None),
    )
    if not commit_error:
        return
    failed = dict(status)
    failed["state"] = "failed"
    existing = str(failed.get("error") or "").strip()
    suffix = f"Modal volume commit failed: {commit_error}"
    failed["error"] = f"{existing}; {suffix}" if existing else suffix
    write_status(args.status, failed)
    commit_volume(
        getattr(args, "volume_mount", None),
        getattr(args, "volume_name", None),
    )


def cancel_requested(path: str) -> bool:
    return os.path.exists(path)


def kill_process_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except ProcessLookupError:
        pass


def terminate_process_group(process: subprocess.Popen) -> None:
    kill_process_group(process, signal.SIGTERM)
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
    if process.poll() is None:
        kill_process_group(process, signal.SIGKILL)
        process.wait()


def cancelled_status(started_at: str | None = None) -> dict:
    return {
        "state": "cancelled",
        "error": None,
        "started_at": started_at,
        "finished_at": now(),
    }


def run(args: argparse.Namespace) -> None:
    if cancel_requested(args.cancel):
        finalize(args, cancelled_status())
        return

    status = {
        "state": "running",
        "error": None,
        "started_at": now(),
        "finished_at": None,
    }
    write_status(args.status, status)
    commit_volume(
        getattr(args, "volume_mount", None),
        getattr(args, "volume_name", None),
    )
    started_at = status["started_at"]
    if cancel_requested(args.cancel):
        finalize(args, cancelled_status(started_at=started_at))
        return

    with open(args.stdout, "ab") as stdout, open(args.stderr, "ab") as stderr:
        process = subprocess.Popen(
            [args.command],
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        with open(args.pid, "w", encoding="utf-8") as handle:
            handle.write(str(process.pid))

        deadline = time.monotonic() + args.timeout
        last_commit = time.monotonic()
        exit_code = None
        while exit_code is None:
            if cancel_requested(args.cancel):
                terminate_process_group(process)
                finalize(args, cancelled_status(started_at=started_at))
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process_group(process)
                process.wait()
                exit_code = -9
                status["error"] = "job timed out"
                break
            try:
                exit_code = process.wait(timeout=min(POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                # Terminal status is still written by finalize().
                if time.monotonic() - last_commit >= COMMIT_INTERVAL_SECONDS:
                    commit_volume(
                        getattr(args, "volume_mount", None),
                        getattr(args, "volume_name", None),
                    )
                    last_commit = time.monotonic()
                continue

        if cancel_requested(args.cancel):
            finalize(args, cancelled_status(started_at=started_at))
            return
        if exit_code == 0:
            status["state"] = "succeeded"
        elif status.get("error") == "job timed out":
            status["state"] = "failed"
        else:
            status["state"] = "failed"
            status["error"] = f"command exited with code {exit_code}"
        status["exit_code"] = exit_code
        status["finished_at"] = now()
        finalize(args, status)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("--pid", required=True)
    parser.add_argument("--cancel", required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--volume-mount", default=None)
    parser.add_argument("--volume-name", default=None)
    args = parser.parse_args()
    try:
        run(args)
    except BaseException:
        finalize(
            args,
            {
                "state": "failed",
                "error": traceback.format_exc(),
                "started_at": None,
                "finished_at": now(),
            },
        )
        raise


if __name__ == "__main__":
    main()
