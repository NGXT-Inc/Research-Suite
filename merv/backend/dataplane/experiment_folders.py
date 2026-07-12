"""Local experiment-folder materialization helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils import ValidationError
from ..workspace import local_experiment_dir


def materialize_experiment_folders(
    *, repo_root: Path, experiments: list[dict[str, Any]]
) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    materialized: list[dict[str, Any]] = []
    for experiment in experiments:
        experiment_id = str(experiment.get("id") or "").strip()
        if not experiment_id:
            continue
        name = str(experiment.get("name") or "").strip()
        folder = local_experiment_dir(
            repo_root=repo_root,
            experiment_id=experiment_id,
            name=name,
        )
        existed = folder.exists()
        if existed and not folder.is_dir():
            raise ValidationError(
                "experiment folder path already exists and is not a directory",
                details={"path": folder.relative_to(repo_root).as_posix()},
            )
        folder.mkdir(parents=True, exist_ok=True)
        materialized.append(
            {
                "experiment_id": experiment_id,
                "name": name,
                "status": str(experiment.get("status") or ""),
                "folder": folder.relative_to(repo_root).as_posix() + "/",
                "created": not existed,
            }
        )
    return {"folders": materialized, "count": len(materialized)}
