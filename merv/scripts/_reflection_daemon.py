"""Isolated brain for agent tool-reflection rounds (dev harness).

Boots a throwaway localhost brain (production ControlApp path, same as
scripts/_feed_demo_server.py) backed by the in-memory FakeSandboxBackend in
*bundled-hardware selection mode*, so an agent experiences the
needs_selection / sandbox.options menu without provisioning a real, paid VM.
State lives in a private --state-dir; the user's live brain and its :8787
port are never touched. The brain is repo-blind — there is no repo_root here;
checkout-local work belongs to the merv-mcp data plane.

Usage:
    python3 scripts/_reflection_daemon.py --state-dir /tmp/merv-reflection --port 9911
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn

from merv.brain.kernel.state import StateStore
from merv.brain.object_storage.blobs import LocalDirBlobStore
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.surface.composition import build_local_server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Brain state directory (default: a fresh temp dir).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9911)
    args = parser.parse_args()

    state_dir = Path(
        args.state_dir or tempfile.mkdtemp(prefix="merv_reflection_")
    ).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    backend = FakeSandboxBackend(
        requires_hardware_selection=True,
        configurable_resources=False,
    )
    server = build_local_server(
        state_dir=state_dir,
        env={},
        execution_backend=backend,
        store=StateStore(db_path=state_dir / "state.sqlite"),
        blobs=LocalDirBlobStore(root=state_dir / "blobs"),
    )
    print(
        f"reflection daemon listening on http://{args.host}:{args.port} "
        f"state={state_dir}",
        flush=True,
    )
    uvicorn.run(server.fastapi_app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
