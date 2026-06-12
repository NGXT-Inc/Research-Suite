"""Workspace-local path helpers shared by domain and execution services."""

from __future__ import annotations

from pathlib import Path


def safe_experiment_dirname(experiment_id: str) -> str:
    """Filesystem-safe directory name for an experiment (its name or id)."""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in experiment_id) or "experiment"


def experiment_folder_rel(*, experiment_id: str, name: str = "") -> str:
    """The experiment folder path relative to the repo root, with trailing slash.

    The agent-facing spelling of local_experiment_dir — used in tool responses
    and workflow guidance so agents are told exactly where to work.
    """
    return f"experiments/{safe_experiment_dirname(name.strip() or experiment_id)}/"


def local_experiment_dir(*, repo_root: Path, experiment_id: str, name: str = "") -> Path:
    """The experiment's one local folder — also its sandbox sync root.

    ``experiments/<name>/`` holds everything the experiment is: plan, code,
    results, report, graph. The folder is named after the experiment's short
    unique name; rows that predate the name requirement fall back to the
    experiment id. The whole folder is pushed to the sandbox at provisioning,
    and while a sandbox is live the local copy is a strict mirror of the
    remote one (pull with --delete: deletions and renames propagate, local
    edits are overwritten).
    """
    return repo_root / "experiments" / safe_experiment_dirname(name.strip() or experiment_id)


def local_sessions_dir(
    *, repo_root: Path, experiment_id: str, sandbox_id: str = ""
) -> Path:
    """Daemon-owned local home for pulled sandbox telemetry, per VM generation.

    MLflow/TensorBoard state and command transcripts are runtime telemetry,
    not experiment content — they live outside the experiment folder, keyed by
    sandbox id so one generation's pull never clobbers another's history.
    """
    base = (
        repo_root
        / ".research_plugin"
        / "sessions"
        / safe_experiment_dirname(experiment_id)
    )
    return base / sandbox_id if sandbox_id else base
