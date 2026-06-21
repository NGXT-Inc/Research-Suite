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


def server_command(
    *,
    launcher: Path,
    research_repo: Path | None,
    store_path: Path | None,
    registry_store_path: Path,
    host: str,
    port: int,
    activity_stderr: bool,
) -> list[str]:
    # Go through the bash launcher so the credential-resolution chain in
    # bin/research-plugin-http (user-config dirs > plugin-tree fallback) runs
    # before backend.transport.http_server starts. If we exec'd `python -m backend.transport.http_server`
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
    if research_repo is not None:
        command.extend(["--repo", str(research_repo)])
    if store_path is not None:
        command.extend(["--store", str(store_path)])
    if activity_stderr:
        command.append("--activity-stderr")
    return command


def start_server(
    plugin_root: Path,
    research_repo: Path | None,
    store_path: Path | None,
    registry_store_path: Path,
    host: str,
    port: int,
    *,
    activity_stderr: bool,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["RESEARCH_PLUGIN_REGISTRY_STORE"] = str(registry_store_path)
    if research_repo is not None:
        env["RESEARCH_PLUGIN_REPO_ROOT"] = str(research_repo)
    if store_path is not None:
        env["RESEARCH_PLUGIN_STORE"] = str(store_path)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    # PYTHONPATH and python interpreter are set by bin/research-plugin-http.
    launcher = plugin_root / "bin" / "research-plugin-http"
    command = server_command(
        launcher=launcher,
        research_repo=research_repo,
        store_path=store_path,
        registry_store_path=registry_store_path,
        host=host,
        port=port,
        activity_stderr=activity_stderr,
    )
    return subprocess.Popen(
        command,
        cwd=research_repo or plugin_root,
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
    parser.add_argument("--repo", default=None, help="Optional legacy single-repo target. Omit for shared multi-project mode.")
    parser.add_argument("--store", default=None, help="SQLite state path for legacy --repo mode. Defaults to <repo>/.research_plugin/state.sqlite.")
    parser.add_argument(
        "--registry-store",
        default=os.environ.get(
            "RESEARCH_PLUGIN_REGISTRY_STORE",
            str(Path.home() / ".research_plugin" / "registry.sqlite"),
        ),
        help="Global registry DB for shared multi-project mode.",
    )
    parser.add_argument("--activity-stderr", action="store_true", help="Mirror activity JSONL events to the backend terminal.")
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()

    plugin_root = Path(__file__).resolve().parents[1]
    research_repo = Path(args.repo).resolve() if args.repo else None
    if research_repo is not None and not research_repo.exists():
        print(f"Target research repo does not exist: {research_repo}", file=sys.stderr)
        return 2
    store_path = None
    if research_repo is not None:
        store_path = Path(args.store).resolve() if args.store else research_repo / ".research_plugin" / "state.sqlite"
    elif args.store:
        print("--store is only valid with legacy --repo mode", file=sys.stderr)
        return 2
    registry_store_path = Path(args.registry_store).expanduser().resolve()
    watch_root = plugin_root / "backend"

    print(f"Watching {watch_root}")
    if research_repo is None:
        print("Mode: shared multi-project")
        print(f"Registry store: {registry_store_path}")
    else:
        print("Mode: legacy single-repo")
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
    proc = start_server(
        plugin_root,
        research_repo,
        store_path,
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
                    research_repo,
                    store_path,
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
                    research_repo,
                    store_path,
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
