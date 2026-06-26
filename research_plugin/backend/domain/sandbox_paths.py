"""Provider-neutral remote sandbox path helpers."""

from __future__ import annotations

import posixpath

from .paths import safe_experiment_dirname

DEFAULT_REMOTE_ROOT = "/workspace"
DEFAULT_DATA_DIR = "/workspace/data"
SESSIONS_DIRNAME = ".research_plugin_sessions"
ARTIFACTS_TO_KEEP_DIRNAME = "artifacts_to_keep"


def remote_experiment_dir(
    *,
    experiment_id: str,
    name: str = "",
    root: str = DEFAULT_REMOTE_ROOT,
    sandbox_uid: str = "",
) -> str:
    """Return the VM work directory for one experiment."""
    folder = safe_experiment_dirname(name.strip() or experiment_id)
    if sandbox_uid:
        folder = safe_experiment_dirname(f"{folder}-{sandbox_uid[:12]}")
    return posixpath.join(root.rstrip("/") or "/", folder)


def remote_sessions_dir(*, experiment_id: str, root: str = DEFAULT_REMOTE_ROOT) -> str:
    """Return the VM telemetry directory for one experiment."""
    return posixpath.join(
        root.rstrip("/") or "/", SESSIONS_DIRNAME, safe_experiment_dirname(experiment_id)
    )


def remote_root_of(experiment_dir: str) -> str:
    """Recover the remote root from a stored per-experiment directory."""
    return posixpath.dirname(experiment_dir.rstrip("/")) or DEFAULT_REMOTE_ROOT
