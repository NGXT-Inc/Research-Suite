"""HTTP daemon process for the Research Plugin backend.

Owns the running uvicorn server: binds the socket, runs uvicorn against the
FastAPI app from `http_api`, and manages the `.research_plugin/daemon.json`
discovery marker over the lifetime of the process.

Also the CLI entry point (`python -m backend.http_server`).
"""

from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

import uvicorn

from .app import ResearchPluginApp
from .daemon_marker import clear_marker, write_marker
from .http_api import create_fastapi_app


def _bind_socket(*, host: str, port: int) -> socket.socket:
    bind_host = host or "127.0.0.1"
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    server_socket = socket.socket(family, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((bind_host, port))
    server_socket.listen(socket.SOMAXCONN)
    server_socket.set_inheritable(True)
    return server_socket


class UvicornHttpServer:
    """uvicorn server wrapper that manages the daemon discovery marker.

    Tests and the CLI launcher get the same lifecycle: bind a socket,
    serve the FastAPI app, write the marker once we're listening, and
    clear it on shutdown.
    """

    def __init__(self, *, app: ResearchPluginApp, host: str, port: int) -> None:
        self._socket = _bind_socket(host=host, port=port)
        selected_port = int(self._socket.getsockname()[1])
        self.server_address = (host, selected_port)
        self._app = app
        self._marker_written = False
        config = uvicorn.Config(
            create_fastapi_app(app=app),
            host=host,
            port=selected_port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)

    def serve_forever(self) -> None:
        # Write the daemon marker as late as possible so other processes only
        # see it once we're actually listening. Best-effort: failures here must
        # not block serving.
        host, port = self.server_address
        try:
            write_marker(repo_root=self._app.store.repo_root, host=host, port=port)
            self._marker_written = True
        except Exception:  # noqa: BLE001
            self._marker_written = False
        try:
            self._server.run(sockets=[self._socket])
        finally:
            self._clear_marker()

    def shutdown(self) -> None:
        self._server.should_exit = True

    def server_close(self) -> None:
        self._clear_marker()
        self._socket.close()

    def _clear_marker(self) -> None:
        if not self._marker_written:
            return
        try:
            clear_marker(repo_root=self._app.store.repo_root)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._marker_written = False


def make_http_server(app: ResearchPluginApp, host: str, port: int) -> UvicornHttpServer:
    return UvicornHttpServer(app=app, host=host, port=port)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("RESEARCH_PLUGIN_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RESEARCH_PLUGIN_HTTP_PORT", "8787")))
    parser.add_argument("--repo", default=os.environ.get("RESEARCH_PLUGIN_REPO_ROOT", "."))
    parser.add_argument("--store", default=os.environ.get("RESEARCH_PLUGIN_STORE", ".research_plugin/state.sqlite"))
    parser.add_argument(
        "--activity-stderr",
        action="store_true",
        default=os.environ.get("RESEARCH_PLUGIN_ACTIVITY_STDERR", "").lower() in {"1", "true", "yes", "on"},
        help="Mirror activity JSONL events to stderr for live terminal watching.",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    db_path = Path(args.store)
    if not db_path.is_absolute():
        db_path = repo_root / db_path

    if args.activity_stderr:
        os.environ["RESEARCH_PLUGIN_ACTIVITY_STDERR"] = "1"

    app = ResearchPluginApp(repo_root=repo_root, db_path=db_path)
    server = make_http_server(app=app, host=args.host, port=args.port)
    host, port = server.server_address
    print(f"research_plugin HTTP API listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
