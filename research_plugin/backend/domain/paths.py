"""Pure logical path names shared by control and data-plane code."""

from __future__ import annotations


def safe_experiment_dirname(experiment_id: str) -> str:
    """Filesystem-safe directory name for an experiment."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in experiment_id) or "experiment"


def experiment_folder_rel(*, experiment_id: str, name: str = "") -> str:
    """Experiment folder path relative to the repo root, with trailing slash."""
    return f"experiments/{safe_experiment_dirname(name.strip() or experiment_id)}/"
