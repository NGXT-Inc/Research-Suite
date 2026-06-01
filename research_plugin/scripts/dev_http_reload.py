#!/usr/bin/env python3
"""Restart the Research Plugin HTTP API when backend source files change."""

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


def start_server(
    plugin_root: Path,
    research_repo: Path,
    store_path: Path,
    host: str,
    port: int,
    *,
    activity_stderr: bool,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(plugin_root)
    env["RESEARCH_PLUGIN_REPO_ROOT"] = str(research_repo)
    env["RESEARCH_PLUGIN_STORE"] = str(store_path)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    python_bin = plugin_root / ".venv" / "bin" / "python"
    command = [
            str(python_bin) if python_bin.exists() else sys.executable,
            "-B",
            "-m",
            "backend.http_server",
            "--host",
            host,
            "--port",
            str(port),
            "--repo",
            str(research_repo),
            "--store",
            str(store_path),
    ]
    if activity_stderr:
        command.append("--activity-stderr")
    return subprocess.Popen(
        command,
        cwd=research_repo,
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
    parser.add_argument("--repo", default=None, help="Target research repo. Defaults to the plugin root for backend development.")
    parser.add_argument("--store", default=None, help="SQLite state path. Defaults to <repo>/.research_plugin/state.sqlite.")
    parser.add_argument("--activity-stderr", action="store_true", help="Mirror activity JSONL events to the backend terminal.")
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parents[1]
    research_repo = Path(args.repo).resolve() if args.repo else plugin_root
    if not research_repo.exists():
        print(f"Target research repo does not exist: {research_repo}", file=sys.stderr)
        return 2
    store_path = Path(args.store).resolve() if args.store else research_repo / ".research_plugin" / "state.sqlite"
    watch_root = plugin_root / "backend"

    print(f"Watching {watch_root}")
    print(f"Research repo: {research_repo}")
    print(f"State store: {store_path}")
    print(f"HTTP API: http://{args.host}:{args.port}")

    if port_in_use(args.host, args.port):
        print(
            f"Port {args.port} is already in use on {args.host}. "
            "Stop the existing HTTP API process or choose another --port.",
            file=sys.stderr,
        )
        return 2

    snapshot = iter_watched_files(watch_root)
    proc = start_server(plugin_root, research_repo, store_path, args.host, args.port, activity_stderr=args.activity_stderr)
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
                proc = start_server(plugin_root, research_repo, store_path, args.host, args.port, activity_stderr=args.activity_stderr)
            if proc.poll() is not None:
                print(f"HTTP API exited with code {proc.returncode}; restarting...", flush=True)
                if port_in_use(args.host, args.port):
                    print(
                        f"Port {args.port} is occupied by another process; not restarting.",
                        file=sys.stderr,
                    )
                    return 2
                proc = start_server(plugin_root, research_repo, store_path, args.host, args.port, activity_stderr=args.activity_stderr)
                snapshot = iter_watched_files(watch_root)
    except KeyboardInterrupt:
        print("Stopping HTTP API...", flush=True)
        stop_server(proc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
