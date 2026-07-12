"""HTTP process for the Merv brain server.

Owns the running uvicorn server: binds the socket and serves the FastAPI app
from the unified brain composition. Local deployment is just this server on
localhost with small-store defaults.
"""

from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path
from typing import Any

import uvicorn

from ..config import Mode, resolve_mode
from ..env import env_bool
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
    """uvicorn server wrapper used by compatibility tests.

    The production launcher builds the unified brain directly. This wrapper is
    a small socket/uvicorn harness for tests and programmatic callers.
    """

    def __init__(
        self,
        *,
        app: Any,
        host: str,
        port: int,
    ) -> None:
        self._socket = _bind_socket(host=host, port=port)
        selected_port = int(self._socket.getsockname()[1])
        self.server_address = (host, selected_port)
        self._app = app
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
        self._server.run(sockets=[self._socket])

    def shutdown(self) -> None:
        self._server.should_exit = True

    def server_close(self) -> None:
        self._socket.close()


def make_http_server(
    app: Any,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> UvicornHttpServer:
    return UvicornHttpServer(app=app, host=host, port=port)


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
    """Run the hosted brain preset.

    Hosted/no-repo-root control requires durable DB, durable blob store, and a
    mounted management key. It has no end-user authentication and is a private
    operator surface.
    """
    from ..composition import build_control_server

    server = build_control_server()
    host, selected_port, uv, server_socket = _serve_uvicorn(
        fastapi_app=server.fastapi_app, host=host, port=port
    )
    print(
        f"merv CONTROL plane listening on http://{host}:{selected_port}",
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


def _serve_local(*, host: str, port: int, state_dir: Path) -> int:
    """Run the localhost brain preset."""
    from ..composition import build_local_server

    server = build_local_server(state_dir=state_dir)
    host, selected_port, uv, server_socket = _serve_uvicorn(
        fastapi_app=server.fastapi_app, host=host, port=port
    )
    print(
        f"merv brain listening on http://{host}:{selected_port}",
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


def control_main() -> int:
    """Launch the hosted brain.

    The console-script entry for the ``control`` extra and the deploy Dockerfile:
    forces control mode (RESEARCH_PLUGIN_MODE=control) so the image entrypoint
    never accidentally binds the local preset. The expiry reaper runs, but the
    broader cleanup sweeps are only built; a managed cron or sidecar must POST
    ``/api/admin/cleanup``. This surface has no end-user authentication and must
    be deployed behind a trusted network boundary.
    """
    os.environ["RESEARCH_PLUGIN_MODE"] = "control"
    return main()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("RESEARCH_PLUGIN_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RESEARCH_PLUGIN_HTTP_PORT", "8787")))
    parser.add_argument(
        "--registry-store",
        default=os.environ.get(
            "RESEARCH_PLUGIN_REGISTRY_STORE",
            str(Path.home() / ".research_plugin" / "registry.sqlite"),
        ),
        help=(
            "Compatibility path whose parent selects the local brain state "
            "root; research records live under the sibling brain/ directory."
        ),
    )
    parser.add_argument(
        "--activity-stderr",
        action="store_true",
        default=env_bool("RESEARCH_PLUGIN_ACTIVITY_STDERR", default=False),
        help=(
            "Legacy compatibility flag. The unified brain exposes bounded "
            "diagnostics over HTTP and does not mirror them to stderr."
        ),
    )
    args = parser.parse_args()

    mode = resolve_mode()
    if mode is Mode.CONTROL:
        return _serve_control(host=args.host, port=args.port)

    if args.activity_stderr:
        os.environ["RESEARCH_PLUGIN_ACTIVITY_STDERR"] = "1"
    return _serve_local(
        host=args.host,
        port=args.port,
        state_dir=Path(args.registry_store).expanduser().resolve().parent / "brain",
    )


if __name__ == "__main__":
    raise SystemExit(main())
