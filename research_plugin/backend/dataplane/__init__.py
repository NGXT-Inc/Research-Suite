"""Data-plane worker: every local-IO duty behind one interface.

The split in docs/CONTROL_DATA_PLANE_SPLIT.md carves the control/data seam
in-process: control-plane code (records, gates, lifecycle) never touches the
local filesystem or local processes directly — it calls a ``DataPlaneWorker``.
The local-mode implementation wraps today's machinery (conn files, local paths,
metrics fallback). Phase 4 adds the task channel — control enqueues, data
executes — which Phase 8 turns into the daemon's long-poll task loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import SandboxLocalState
    from .tasks import InProcessTaskChannel, Task
    from .worker import DataPlaneWorker, LocalDataPlaneWorker

__all__ = [
    "DataPlaneWorker",
    "InProcessTaskChannel",
    "LocalDataPlaneWorker",
    "SandboxLocalState",
    "Task",
]


def __getattr__(name: str) -> Any:
    """Preserve package exports without eagerly importing local-IO workers."""
    if name == "SandboxLocalState":
        from .state import SandboxLocalState

        value = SandboxLocalState
    elif name in {"InProcessTaskChannel", "Task"}:
        from .tasks import InProcessTaskChannel, Task

        value = {"InProcessTaskChannel": InProcessTaskChannel, "Task": Task}[name]
    elif name in {"DataPlaneWorker", "LocalDataPlaneWorker"}:
        from .worker import DataPlaneWorker, LocalDataPlaneWorker

        value = {
            "DataPlaneWorker": DataPlaneWorker,
            "LocalDataPlaneWorker": LocalDataPlaneWorker,
        }[name]
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
