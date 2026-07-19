#!/usr/bin/env python3
"""Restart the Merv HTTP API when backend source files change."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def iter_watched_files(root: Path) -> dict[Path, int]:
    files: dict[Path, int] = {}
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            files[path] = path.stat().st_mtime_ns
        except FileNotFoundError:
            continue
    return files


def port_in_use(host: str, port: int) -> bool:
    if port == 0:
        return False
    connect_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((connect_host, port)) == 0


def server_command(
    *,
    launcher: Path,
    registry_store_path: Path,
    host: str,
    port: int,
    activity_stderr: bool,
) -> list[str]:
    # Go through the bash launcher so the credential-resolution chain in
    # bin/merv-http (user-config dirs > plugin-tree fallback) runs
    # before merv.brain.surface.transport.http_server starts. If we exec'd `python -m merv.brain.surface.transport.http_server`
    # directly here, RESEARCH_PLUGIN_MODAL_ENV_FILE never gets set and the
    # Modal client breaks because nothing populates MODAL_TOKEN_ID/SECRET.
    command = [
        str(launcher),
        "--host",
        host,
        "--port",
        str(port),
        "--registry-store",
        str(registry_store_path),
    ]
    if activity_stderr:
        command.append("--activity-stderr")
    return command


def start_server(
    plugin_root: Path,
    registry_store_path: Path,
    host: str,
    port: int,
    *,
    activity_stderr: bool,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["MERV_REGISTRY_STORE"] = str(registry_store_path)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    # PYTHONPATH and python interpreter are set by bin/merv-http.
    launcher = plugin_root / "bin" / "merv-http"
    command = server_command(
        launcher=launcher,
        registry_store_path=registry_store_path,
        host=host,
        port=port,
        activity_stderr=activity_stderr,
    )
    return subprocess.Popen(
        command,
        cwd=plugin_root,
        env=env,
    )


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--registry-store",
        default=os.environ.get("MERV_REGISTRY_STORE")
        or os.environ.get("RESEARCH_PLUGIN_REGISTRY_STORE"),
        help=(
            "Global registry DB for shared multi-project mode; unset resolves "
            "next to the brain staging root (~/.merv, legacy-aware)."
        ),
    )
    parser.add_argument(
        "--activity-stderr",
        action="store_true",
        help=(
            "Pass the legacy compatibility flag to the brain; current "
            "diagnostics are available over HTTP instead."
        ),
    )
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parents[1]
    if args.registry_store:
        registry_store_path = Path(args.registry_store).expanduser().resolve()
    else:
        # Match the brain's own resolution: <staging-parent>/registry.sqlite
        # so the child's `<registry>.parent / "brain"` lands on the same root.
        sys.path.insert(0, str(plugin_root / "src"))
        from merv.brain.surface.composition.brain_dirs import resolve_local_brain_staging

        registry_store_path = (
            resolve_local_brain_staging().parent / "registry.sqlite"
        ).expanduser().resolve()
    watch_root = plugin_root / "src" / "merv" / "brain"

    print(f"Watching {watch_root}")
    print("Mode: shared multi-project")
    print(f"Registry store: {registry_store_path}")
    print(f"HTTP API: http://{args.host}:{args.port}")

    if port_in_use(args.host, args.port):
        print(
            f"Port {args.port} is already in use on {args.host}. "
            "Stop the existing HTTP API process or choose another --port.",
            file=sys.stderr,
        )
        return 2

    snapshot = iter_watched_files(watch_root)
    proc = start_server(
        plugin_root,
        registry_store_path,
        args.host,
        args.port,
        activity_stderr=args.activity_stderr,
    )
    try:
        while True:
            time.sleep(args.interval)
            current = iter_watched_files(watch_root)
            if current != snapshot:
                print("Backend change detected; restarting HTTP API...", flush=True)
                stop_server(proc)
                snapshot = current
                if port_in_use(args.host, args.port):
                    print(
                        f"Port {args.port} is still in use after stopping the child server; not restarting.",
                        file=sys.stderr,
                    )
                    return 2
                proc = start_server(
                    plugin_root,
                    registry_store_path,
                    args.host,
                    args.port,
                    activity_stderr=args.activity_stderr,
                )
            if proc.poll() is not None:
                print(f"HTTP API exited with code {proc.returncode}; restarting...", flush=True)
                if port_in_use(args.host, args.port):
                    print(
                        f"Port {args.port} is occupied by another process; not restarting.",
                        file=sys.stderr,
                    )
                    return 2
                proc = start_server(
                    plugin_root,
                    registry_store_path,
                    args.host,
                    args.port,
                    activity_stderr=args.activity_stderr,
                )
                snapshot = iter_watched_files(watch_root)
    except KeyboardInterrupt:
        print("Stopping HTTP API...", flush=True)
        stop_server(proc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
