"""Pure logical path names shared by control and data-plane code."""

from __future__ import annotations

from ...kernel.utils import safe_experiment_dirname


def experiment_folder_rel(*, experiment_id: str, name: str = "") -> str:
    """Experiment folder path relative to the repo root, with trailing slash."""
    return f"experiments/{safe_experiment_dirname(name.strip() or experiment_id)}/"
