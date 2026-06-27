"""The DataPlaneWorker interface and its local-mode implementation.

Every local-IO duty of the sandbox stack routes through this seam: workspace
folders, SSH keypairs, and conn files.
Control-plane code (registry, provisioner, facade verbs) calls the interface;
``LocalDataPlaneWorker`` binds it to this machine by wrapping the existing
machinery. In split mode the same duties become the daemon's task loop
(Phase 8). Sandbox file movement is not a backend service: the agent uses the
SSH credentials directly to copy out anything it wants to keep before release.

Since plan Phase 5's management-key switch, ``read_transcript`` and live
usage sampling are control-plane duties authenticated by the per-sandbox
management key; the worker-held user key is data-plane-only (the sbx
dispatcher and copy-out workflows).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES
from ..workspace import LocalWorkspace
from .sandbox_conn import SandboxConnFiles
from .state import SandboxLocalState


class DataPlaneWorker(Protocol):
    """Local-IO duties the control plane is never allowed to perform itself."""

    workspace: LocalWorkspace

    def client_id(self) -> str: ...

    def ensure_workspace(self, *, experiment_id: str, name: str = "") -> Path: ...

    def local_experiment_dir(self, *, experiment_id: str, name: str = "") -> Path: ...

    def repo_relative(self, path: str | Path) -> str: ...

    def ensure_keypair(self, *, experiment_id: str) -> tuple[str, Path]: ...

    def key_path(self, *, experiment_id: str) -> Path: ...

    def remove_conn_file(
        self,
        *,
        experiment_id: str,
        sandbox_uid: str = "",
        remove_experiment_alias: bool = True,
    ) -> None: ...

    def sandbox_enrichment(
        self,
        *,
        row: dict[str, Any],
        name: str = "",
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]: ...


class LocalDataPlaneWorker:
    """Local-mode worker: today's sandbox IO machinery behind the seam.

    Wraps ``SandboxConnFiles`` (keys, dispatcher, conn files); machine-local
    sandbox facts (key path, local folder) persist in ``SandboxLocalState``,
    never in cloud-bound rows.
    """

    def __init__(
        self,
        *,
        workspace: LocalWorkspace,
    ) -> None:
        self.workspace = workspace
        keys_dir = workspace.research_dir / "sandboxes" / "keys"
        self._conn = SandboxConnFiles(repo_root=workspace.repo_root, keys_dir=keys_dir)
        self.state = SandboxLocalState(
            db_path=workspace.research_dir / "dataplane_state.sqlite"
        )

    # ---------- identity ----------

    def client_id(self) -> str:
        """Stable data-plane client identity for daemon polling."""
        return self.state.client_id()

    # ---------- workspace ----------

    def ensure_workspace(self, *, experiment_id: str, name: str = "") -> Path:
        """Create the experiment's local folder."""
        folder = self.workspace.experiment_dir(experiment_id=experiment_id, name=name)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def local_experiment_dir(
        self, *, experiment_id: str, name: str = "", sandbox_uid: str = ""
    ) -> Path:
        if sandbox_uid:
            # Additional sandbox: a uid-suffixed local dir matches the remote
            # root so explicit copy-outs never overwrite the primary folder.
            base = self.workspace.experiment_dir(experiment_id=experiment_id, name=name)
            return base.with_name(f"{base.name}-{sandbox_uid[:12]}")
        stored = self.state.load(experiment_id=experiment_id)["local_sync_dir"]
        if stored:
            return Path(stored)
        return self.workspace.experiment_dir(experiment_id=experiment_id, name=name)

    def repo_relative(self, path: str | Path) -> str:
        return self.workspace.relative(path)

    # ---------- keys / conn files ----------

    def ensure_keypair(self, *, experiment_id: str) -> tuple[str, Path]:
        public_key, key_path = self._conn.ensure_keypair(experiment_id=experiment_id)
        self.state.record(experiment_id=experiment_id, key_path=str(key_path))
        return public_key, key_path

    def key_path(self, *, experiment_id: str) -> Path:
        return self._conn.key_path(experiment_id=experiment_id)

    def remove_conn_file(
        self,
        *,
        experiment_id: str,
        sandbox_uid: str = "",
        remove_experiment_alias: bool = True,
    ) -> None:
        self._conn.remove_conn(
            experiment_id=experiment_id,
            sandbox_uid=sandbox_uid,
            remove_experiment_alias=remove_experiment_alias,
        )

    def sandbox_enrichment(
        self,
        *,
        row: dict[str, Any],
        name: str = "",
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        """The machine-local half of the agent view (plan §3.3).

        Writes/refreshes the conn file for a live row and renders the ssh
        command, raw command, key path, and local folder. The control plane
        merges this with the row facts; in split mode the daemon supplies it.
        """
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        local_key = sandbox_uid or experiment_id
        key_path = self.key_path(experiment_id=local_key)
        live = bool(
            row.get("ssh_host")
            and row.get("ssh_port")
            and (row.get("status") or "none") in ACTIVE_SANDBOX_STATUSES
        )
        command = (
            self._conn.write_command_wrapper(
                row=row,
                key_path=key_path,
                use_sandbox_uid_command=use_sandbox_uid_command,
            )
            if live
            else ""
        )
        raw_command = (
            self._conn.raw_ssh_command(row=row, key_path=key_path) if live else ""
        )
        return {
            "key_path": str(key_path),
            "command": command,
            "raw_command": raw_command,
            "local_dir": str(
                self.local_experiment_dir(
                    experiment_id=local_key,
                    name=name,
                )
            ),
        }
