"""Mode composition roots (cloud plan Phase 8, fixed decision 1).

Mode is selected in composition only; services are mode-blind. One repo, three
process roles:

- ``local_mode``  — today's topology, both planes in one process (the default,
  byte-identical to before this phase).
- ``control_mode`` — the cloud control plane: record services + lifecycle +
  blob store + quotas + the daemon task HTTP endpoints.
  It serves /mcp/* (control tools) + /api/* but NEVER touches a user checkout.
- ``daemon_mode`` — the slim local data-plane daemon: LocalDataPlaneWorker +
  HttpControlPlaneClient to the cloud, the task long-poll loop, and the local
  data-plane tool subset.

``http_server.main`` dispatches on ``resolve_mode`` to the right builder. Each
builder owns its own fail-fast validation (a daemon without a control URL
refuses to start).
"""

from __future__ import annotations

from .control_mode import ControlPlaneServer, build_control_app, build_control_server
from .daemon_mode import DaemonServer, build_daemon_server
from .local_mode import build_local_app

__all__ = [
    "ControlPlaneServer",
    "DaemonServer",
    "build_control_app",
    "build_control_server",
    "build_daemon_server",
    "build_local_app",
]
