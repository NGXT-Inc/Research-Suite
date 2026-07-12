"""Brain composition roots.

There is one app composition: ``ControlApp``. ``RESEARCH_PLUGIN_MODE`` selects
deployment defaults only: localhost SQLite/dir blobs/local management keys, or
hosted durable stores/mounted management keys.
"""

from __future__ import annotations

from .control_mode import (
    ControlPlaneServer,
    build_control_app,
    build_control_server,
    build_local_server,
)

__all__ = [
    "ControlPlaneServer",
    "build_control_app",
    "build_control_server",
    "build_local_server",
]
