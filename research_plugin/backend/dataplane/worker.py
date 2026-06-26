"""The DataPlaneWorker interface and its local-mode implementation.

Every local-IO duty of the sandbox stack routes through this seam: workspace
folders, SSH keypairs and conn files, dashboard tunnels, and the legacy pulled-
``mlflow.db`` metrics fallback.
Control-plane code (registry, provisioner, facade verbs) calls the interface;
``LocalDataPlaneWorker`` binds it to this machine by wrapping the existing
machinery. In split mode the same duties become the daemon's task loop
(Phase 8). Sandbox file movement is not a backend service: the agent uses the
SSH credentials directly to copy out anything it wants to keep before release.

Since plan Phase 5's management-key switch, ``read_transcript`` and
``sample_metrics`` are control-plane duties authenticated by the per-sandbox
management key; the worker-held user key is data-plane-only (the sbx
dispatcher and dashboard tunnels).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol

from ..sandbox.sandbox_backend import SandboxBackend
from .metrics_archive import MetricsArchive, snapshot_mlflow_db
from .sandbox_dashboards import DashboardTunnels
from ..sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES
from ..workspace import LocalWorkspace
from .mlflow_tunnels import MlflowReverseTunnels
from .sandbox_conn import SandboxConnFiles
from .state import SandboxLocalState


class DataPlaneWorker(Protocol):
    """Local-IO duties the control plane is never allowed to perform itself."""

    workspace: LocalWorkspace
    metrics_archive: MetricsArchive

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
        use_sandbox_uid_command: bool = False,
    ) -> dict[str, Any]: ...

    def ensure_local_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]: ...

    def merge_local_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]: ...

    def stop_dashboards(self, *, sandbox_id: str = "") -> None: ...

    def ensure_mlflow_access(
        self, *, row: dict[str, Any], tracking_uri: str
    ) -> dict[str, Any]: ...

    def stop_mlflow_access(self, *, sandbox_id: str = "") -> None: ...

    def pulled_mlflow_db_path(self, *, experiment_id: str, name: str = "") -> Path: ...

    def capture_metrics_fallback(
        self, *, experiment_id: str, name: str = ""
    ) -> dict[str, Any] | None: ...

    def capture_metrics_snapshot(
        self, *, row: dict[str, Any], name: str = ""
    ) -> dict[str, Any] | None: ...

    def set_event_sink(self, emit_event: Callable[..., None]) -> None: ...


class LocalDataPlaneWorker:
    """Local-mode worker: today's sandbox IO machinery behind the seam.

    Wraps ``SandboxConnFiles`` (keys, dispatcher, conn files),
    ``DashboardTunnels`` (ssh -L pool), and ``MetricsArchive``; machine-local
    sandbox facts (key path, local folder, loopback dashboard URLs) persist in
    ``SandboxLocalState``, never in cloud-bound rows.
    """

    def __init__(
        self,
        *,
        workspace: LocalWorkspace,
        backend: SandboxBackend,
    ) -> None:
        self.workspace = workspace
        keys_dir = workspace.research_dir / "sandboxes" / "keys"
        self._conn = SandboxConnFiles(repo_root=workspace.repo_root, keys_dir=keys_dir)
        self.state = SandboxLocalState(
            db_path=workspace.research_dir / "dataplane_state.sqlite"
        )
        self.metrics_archive = MetricsArchive(repo_root=workspace.repo_root)
        self.dashboards = DashboardTunnels(
            backend=backend,
            key_path=self.key_path,
            local_state=self.state,
        )
        self.mlflow_tunnels = MlflowReverseTunnels(key_path=self.key_path)

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

    @staticmethod
    def _local_dir_uid(*, remote_dir: str, sandbox_uid: str) -> str:
        """The uid to suffix the local dir with — set only for an additional
        sandbox, whose remote dir carries the uid suffix (sync_contract)."""
        uid = (sandbox_uid or "")[:12]
        return sandbox_uid if uid and remote_dir.rstrip("/").endswith(f"-{uid}") else ""

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
        use_sandbox_uid_command: bool = False,
    ) -> dict[str, Any]:
        """The machine-local half of the agent view (plan §3.3).

        Writes/refreshes the conn file for a live row and renders the ssh
        command, raw command, key path, and local folder. The control plane
        merges this with the row facts; in split mode the daemon supplies it.
        """
        experiment_id = str(row.get("experiment_id") or "")
        key_path = self.key_path(experiment_id=experiment_id)
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
                    experiment_id=experiment_id,
                    name=name,
                    sandbox_uid=self._local_dir_uid(
                        remote_dir=str(row.get("workdir") or row.get("sync_dir") or ""),
                        sandbox_uid=str(row.get("sandbox_uid") or ""),
                    ),
                )
            ),
        }

    # ---------- dashboards ----------

    def ensure_local_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return self.dashboards.ensure_local(row=row)

    def merge_local_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return self.dashboards.merged_row(row=row)

    def stop_dashboards(self, *, sandbox_id: str = "") -> None:
        self.dashboards.stop(sandbox_id=sandbox_id)

    def ensure_mlflow_access(
        self, *, row: dict[str, Any], tracking_uri: str
    ) -> dict[str, Any]:
        return self.mlflow_tunnels.ensure(row=row, tracking_uri=tracking_uri)

    def stop_mlflow_access(self, *, sandbox_id: str = "") -> None:
        self.mlflow_tunnels.stop(sandbox_id=sandbox_id)

    # ---------- pulled-metrics fallback ----------

    def pulled_mlflow_db_path(self, *, experiment_id: str, name: str = "") -> Path:
        # Legacy sandbox-local MLflow backend store. Current experiments should
        # use centralized MLflow; older local copies are checked as fallbacks so
        # pre-centralization runs keep their lazy metrics backfill.
        sessions_base = self.workspace.sessions_dir(experiment_id=experiment_id)
        candidates = sorted(
            sessions_base.glob("*/mlflow.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
        local_dir = self.local_experiment_dir(experiment_id=experiment_id, name=name)
        for legacy in (
            local_dir / ".research_plugin_sessions" / experiment_id / "mlflow.db",
            local_dir / "synced" / ".research_plugin_sessions" / experiment_id / "mlflow.db",
        ):
            if legacy.exists():
                return legacy
        return sessions_base / "mlflow.db"

    def capture_metrics_fallback(
        self, *, experiment_id: str, name: str = ""
    ) -> dict[str, Any] | None:
        snapshot = snapshot_mlflow_db(
            self.pulled_mlflow_db_path(experiment_id=experiment_id, name=name)
        )
        return self._portable_metrics_snapshot(snapshot=snapshot)

    def capture_metrics_snapshot(
        self, *, row: dict[str, Any], name: str = ""
    ) -> dict[str, Any] | None:
        experiment_id = str(row.get("experiment_id") or "")
        return self.capture_metrics_fallback(experiment_id=experiment_id, name=name)

    def _portable_metrics_snapshot(
        self, *, snapshot: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not isinstance(snapshot, dict):
            return None
        portable = dict(snapshot)
        portable.pop("base_url", None)
        extracted_from = snapshot.get("extracted_from")
        if not isinstance(extracted_from, str) or not extracted_from:
            return portable
        portable["extracted_from"] = self.repo_relative(extracted_from)
        return portable

    # ---------- record-sink wiring ----------

    def set_event_sink(self, emit_event: Callable[..., None]) -> None:
        """Bind the control-plane event recorder (registry.emit_event).

        Data-plane work that deserves a record (a dashboard tunnel came up)
        reports through this hook; Phase 4's task channel formalizes it.
        """
        self.dashboards.emit_event = emit_event
        self.mlflow_tunnels.emit_event = emit_event
