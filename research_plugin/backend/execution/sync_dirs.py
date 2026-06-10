"""Provider-neutral remote directory contract for SSH sandboxes."""

from __future__ import annotations

from pathlib import Path


DEFAULT_SYNC_DIR = "/workspace/synced"
DEFAULT_UNSYNCED_DIR = "/workspace/unsynced"
ARTIFACTS_TO_KEEP_DIRNAME = "artifacts_to_keep"


def safe_experiment_dirname(experiment_id: str) -> str:
    """Filesystem-safe directory name for an experiment id."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in experiment_id) or "experiment"


def local_experiment_sync_dir(*, repo_root: Path, experiment_id: str) -> Path:
    return repo_root / "experiments" / safe_experiment_dirname(experiment_id) / "synced"


def sync_hint() -> str:
    return (
        "Use /workspace/synced for code, logs, metrics, and small outputs that "
        "should be rsynced back to the local experiment folder. Use "
        "/workspace/unsynced for datasets, caches, temporary checkpoints, and "
        "large scratch files. Put deliberate large final artifacts under "
        "/workspace/synced/artifacts_to_keep."
    )
