"""Workspace-local path helpers shared by domain and execution services."""

from __future__ import annotations

from pathlib import Path


def safe_experiment_dirname(experiment_id: str) -> str:
    """Filesystem-safe directory name for an experiment id."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in experiment_id) or "experiment"


def local_experiment_sync_dir(*, repo_root: Path, experiment_id: str) -> Path:
    return repo_root / "experiments" / safe_experiment_dirname(experiment_id) / "synced"
