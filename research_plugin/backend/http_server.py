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
from .config import (
    Mode,
    resolve_control_token,
    resolve_control_url,
    resolve_mode,
)
from .daemon_marker import clear_marker, write_marker
from .http_api import create_fastapi_app
from .project_router import ProjectRouter


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

    def __init__(
        self,
        *,
        app: ResearchPluginApp | None = None,
        router: ProjectRouter | None = None,
        host: str,
        port: int,
    ) -> None:
        if (app is None) == (router is None):
            raise ValueError("provide exactly one of app or router")
        self._socket = _bind_socket(host=host, port=port)
        selected_port = int(self._socket.getsockname()[1])
        self.server_address = (host, selected_port)
        self._app = app
        self._router = router
        self._marker_written = False
        config = uvicorn.Config(
            create_fastapi_app(app=app, router=router),
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
        if self._app is not None:
            try:
                write_marker(repo_root=self._app.workspace.repo_root, host=host, port=port)
                self._marker_written = True
            except Exception:  # noqa: BLE001
                self._marker_written = False
        elif self._router is not None:
            self._router.set_marker_endpoint(host=host, port=port)
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
        if self._router is not None:
            self._router.clear_markers()
            return
        if not self._marker_written or self._app is None:
            return
        try:
            clear_marker(repo_root=self._app.workspace.repo_root)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._marker_written = False


def make_http_server(
    app: ResearchPluginApp | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    router: ProjectRouter | None = None,
) -> UvicornHttpServer:
    return UvicornHttpServer(app=app, router=router, host=host, port=port)


def _serve_uvicorn(*, fastapi_app, host: str, port: int) -> tuple[str, int, "uvicorn.Server", socket.socket]:
    server_socket = _bind_socket(host=host, port=port)
    selected_port = int(server_socket.getsockname()[1])
    config = uvicorn.Config(
        fastapi_app,
        host=host,
        port=selected_port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    return host, selected_port, uvicorn.Server(config), server_socket


def _serve_control(*, host: str, port: int) -> int:
    """Run the cloud control-plane composition (cloud plan Phase 8).

    Postgres when RESEARCH_PLUGIN_DB_URL is set, else SQLite (fine for dev, not
    multi-tenant production — documented in the composition root). Auth is ON.
    """
    from .composition import build_control_server

    server = build_control_server()
    host, selected_port, uv, server_socket = _serve_uvicorn(
        fastapi_app=server.fastapi_app, host=host, port=port
    )
    print(
        f"research_plugin CONTROL plane listening on http://{host}:{selected_port}",
        flush=True,
    )
    try:
        uv.run(sockets=[server_socket])
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server_socket.close()
    return 0


def _serve_daemon(*, host: str, port: int) -> int:
    """Run the slim local data-plane daemon (cloud plan Phase 8, §3.4).

    Fail-fast: refuses to start without RESEARCH_PLUGIN_CONTROL_URL (no silent
    127.0.0.1 fallback). Starts the task long-poll + auto-sync loops and serves
    a loopback surface for the proxy (GET /local/route + the data-plane tools).
    """
    from .composition import build_daemon_server
    from .daemon_loopback import create_daemon_loopback_app

    control_url = resolve_control_url()
    token = resolve_control_token()
    daemon = build_daemon_server(control_url=control_url, token=token)
    daemon.start()
    loopback = create_daemon_loopback_app(daemon=daemon)
    host, selected_port, uv, server_socket = _serve_uvicorn(
        fastapi_app=loopback, host=host, port=port
    )
    print(
        f"research_plugin DAEMON (data plane) listening on "
        f"http://{host}:{selected_port}; upstream {control_url}",
        flush=True,
    )
    try:
        uv.run(sockets=[server_socket])
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
        server_socket.close()
    return 0


def daemon_main() -> int:
    """Launch the slim data-plane daemon (cloud plan Phase 8, §3.4).

    The console-script entry for the ``daemon`` extra: forces daemon mode
    (RESEARCH_PLUGIN_MODE=daemon) so it never accidentally binds the control or
    local topology, and does NOT import any provider SDK at startup. Fail-fast
    on a missing control URL lives in the composition root.
    """
    os.environ["RESEARCH_PLUGIN_MODE"] = "daemon"
    return main()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("RESEARCH_PLUGIN_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RESEARCH_PLUGIN_HTTP_PORT", "8787")))
    parser.add_argument("--repo", default=os.environ.get("RESEARCH_PLUGIN_REPO_ROOT"))
    parser.add_argument("--store", default=os.environ.get("RESEARCH_PLUGIN_STORE", ".research_plugin/state.sqlite"))
    parser.add_argument(
        "--registry-store",
        default=os.environ.get(
            "RESEARCH_PLUGIN_REGISTRY_STORE",
            str(Path.home() / ".research_plugin" / "registry.sqlite"),
        ),
        help="Global registry DB for shared multi-project mode.",
    )
    parser.add_argument(
        "--activity-stderr",
        action="store_true",
        default=os.environ.get("RESEARCH_PLUGIN_ACTIVITY_STDERR", "").lower() in {"1", "true", "yes", "on"},
        help="Mirror activity JSONL events to stderr for live terminal watching.",
    )
    args = parser.parse_args()

    # Fail fast on a wrong/unsupported RESEARCH_PLUGIN_MODE rather than
    # silently starting in the wrong topology. Mode dispatch (cloud plan
    # Phase 8): local stays the byte-identical default path below; control and
    # daemon route to their composition roots.
    mode = resolve_mode()
    if mode is Mode.CONTROL:
        return _serve_control(host=args.host, port=args.port)
    if mode is Mode.DAEMON:
        return _serve_daemon(host=args.host, port=args.port)

    if args.activity_stderr:
        os.environ["RESEARCH_PLUGIN_ACTIVITY_STDERR"] = "1"

    app: ResearchPluginApp | None = None
    router: ProjectRouter | None = None
    if args.repo:
        repo_root = Path(args.repo).resolve()
        db_path = Path(args.store)
        if not db_path.is_absolute():
            db_path = repo_root / db_path
        app = ResearchPluginApp(repo_root=repo_root, db_path=db_path)
        server = make_http_server(app=app, host=args.host, port=args.port)
    else:
        router = ProjectRouter(registry_db_path=Path(args.registry_store))
        server = make_http_server(router=router, host=args.host, port=args.port)
    host, port = server.server_address
    print(f"research_plugin HTTP API listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if app is not None:
            app.shutdown()
        if router is not None:
            router.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
