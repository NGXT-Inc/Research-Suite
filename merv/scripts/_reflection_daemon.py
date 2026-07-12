"""Isolated daemon for agent tool-reflection rounds (temporary harness).

Boots a Merv HTTP daemon scoped to a throwaway project directory and
backed by the in-memory FakeSandboxBackend in *bundled-hardware selection mode*
(so an agent experiences the Lambda-style needs_selection / sandbox.options menu
without provisioning a real, paid VM). Never touches the user's live project or
its :8787 daemon.

Usage:
    .venv/bin/python scripts/_reflection_daemon.py --project-dir /tmp/rp-reflection/proj --port 9911
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.transport.http_server import make_http_server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9911)
    args = parser.parse_args()

    repo_root = Path(args.project_dir).resolve()
    repo_root.mkdir(parents=True, exist_ok=True)
    db_path = repo_root / ".research_plugin" / "state.sqlite"

    backend = FakeSandboxBackend(
        requires_hardware_selection=True,
        configurable_resources=False,
    )
    app = ResearchPluginApp(
        repo_root=repo_root,
        db_path=db_path,
        execution_backend=backend,
    )
    server = make_http_server(app=app, host=args.host, port=args.port)
    host, port = server.server_address
    print(f"reflection daemon listening on http://{host}:{port} repo={repo_root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
