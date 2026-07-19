"""Workspace-local path helpers shared by domain and execution services."""

from __future__ import annotations

from pathlib import Path

from merv.shared.project_dirs import resolve_project_state_dir

from .kernel.utils import safe_experiment_dirname


class LocalWorkspace:
    """Data-plane view of the local repository checkout.

    Owns the repo root and every path derived from it. The control-plane
    record layer (``StateStore``) does not know where the checkout lives;
    anything that needs a local path receives this object — or the
    ``DataPlaneWorker`` built on it — from the composition root.
    """

    def __init__(self, *, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    @property
    def research_dir(self) -> Path:
        return resolve_project_state_dir(self.repo_root)

    def experiment_dir(self, *, experiment_id: str, name: str = "") -> Path:
        return local_experiment_dir(
            repo_root=self.repo_root, experiment_id=experiment_id, name=name
        )

    def sessions_dir(self, *, experiment_id: str, sandbox_id: str = "") -> Path:
        return local_sessions_dir(
            repo_root=self.repo_root, experiment_id=experiment_id, sandbox_id=sandbox_id
        )

    def relative(self, path: str | Path) -> str:
        """Repo-relative spelling of a local path.

        Event payloads and other cloud-bound records must not carry absolute
        machine paths; a path outside the repo passes through unchanged.
        """
        if not str(path):
            return ""
        try:
            return Path(path).resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)


def local_experiment_dir(*, repo_root: Path, experiment_id: str, name: str = "") -> Path:
    """The experiment's one local folder.

    ``experiments/<name>/`` holds everything the experiment is: plan, code,
    results, report, graph. The folder is named after the experiment's short
    unique name; rows that predate the name requirement fall back to the
    experiment id. Sandbox files are retained deliberately: copy light files
    back into this folder over SSH and upload heavy files to durable storage
    before releasing the VM.
    """
    return repo_root / "experiments" / safe_experiment_dirname(name.strip() or experiment_id)


def local_sessions_dir(
    *, repo_root: Path, experiment_id: str, sandbox_id: str = ""
) -> Path:
    """Machine-local home for pulled sandbox telemetry, per VM generation.

    Command transcripts and legacy sandbox-local MLflow state are runtime
    telemetry, not experiment content — they live outside the
    experiment folder, keyed by sandbox id so one generation's pull never
    clobbers another's history.
    """
    base = (
        resolve_project_state_dir(repo_root)
        / "sessions"
        / safe_experiment_dirname(experiment_id)
    )
    return base / sandbox_id if sandbox_id else base
