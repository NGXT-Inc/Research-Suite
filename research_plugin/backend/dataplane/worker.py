"""The DataPlaneWorker interface and its local-mode implementation.

Every local-IO duty of the sandbox stack routes through this seam (cloud plan
§3.1): workspace folders, SSH keypairs and conn files, the initial rsync push,
sync pulls, dashboard tunnels, and the pulled-``mlflow.db`` metrics fallback.
Control-plane code (registry, provisioner, facade verbs) calls the interface;
``LocalDataPlaneWorker`` binds it to this machine by wrapping the existing
machinery. In split mode the same duties become the daemon's task loop
(Phase 8).

Byte movement is session-shaped (plan Phase 4): ``push_initial``/``sync_pull``
/``final_pull`` take the lease-backed sync session the control plane minted —
SSH endpoint, remote directory contract, ``direction_policy`` — and refuse a
session whose policy or contract version the local rsync flags do not
implement. The worker's per-experiment locks serialize rsync on this machine,
subordinate to the lease (the cross-client authority).

Since plan Phase 5's management-key switch, ``read_transcript`` and
``sample_metrics`` are control-plane duties authenticated by the per-sandbox
management key; the worker-held user key is data-plane-only (rsync, the sbx
dispatcher, dashboard tunnels).
"""

from __future__ import annotations

import io
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from ..execution.ssh_rsync import SshRsyncSyncer
from ..env import env_float
from ..sandbox_backend import SandboxBackend
from .metrics_archive import MetricsArchive, snapshot_mlflow, snapshot_mlflow_db
from .sandbox_dashboards import DashboardTunnels
from ..sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    DEFAULT_INITIAL_PUSH_ATTEMPTS,
    DEFAULT_INITIAL_PUSH_RETRY_SECONDS,
    decode_dashboards,
)
from ..domain.sync_contract import (
    DIRECTION_POLICY,
    SYNC_SESSION_SCHEMA_VERSION,
    TRANSFER_CONTRACT_VERSION,
)
from ..utils import ValidationError
from ..workspace import LocalWorkspace
from .sandbox_conn import SandboxConnFiles
from .state import SandboxLocalState


# (attempt, attempts) — progress hook for the initial-push retry loop, so the
# provisioner can surface "waiting for remote workspace" without the worker
# knowing about provision rows.
OnPushRetry = Callable[[int, int], None]


def _require_session(session: Any) -> dict[str, Any]:
    """Refuse byte movement outside the transfer contract (plan Phase 4).

    The session's ``direction_policy`` must be exactly the one this worker's
    rsync flags implement — experiment dir mirrored remote-authoritative
    (pull with --delete), ``artifacts_to_keep`` on its own append-only-shaped
    5 GB pass — and the contract version must match, so a session minted
    under different rules fails loudly instead of moving bytes wrong.
    """
    if not isinstance(session, dict) or not str(session.get("experiment_id") or ""):
        raise ValidationError("sync session is required for sandbox byte movement")
    if int(session.get("schema_version") or 0) != SYNC_SESSION_SCHEMA_VERSION:
        raise ValidationError(
            f"unsupported sync session schema_version: {session.get('schema_version')!r}"
        )
    if int(session.get("transfer_contract_version") or 0) != TRANSFER_CONTRACT_VERSION:
        raise ValidationError(
            "unsupported transfer_contract_version: "
            f"{session.get('transfer_contract_version')!r} "
            f"(this worker implements {TRANSFER_CONTRACT_VERSION})"
        )
    if dict(session.get("direction_policy") or {}) != DIRECTION_POLICY:
        raise ValidationError(
            f"unsupported direction_policy: {session.get('direction_policy')!r} "
            f"(this worker's rsync implements {DIRECTION_POLICY})"
        )
    return session


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

    def remove_conn_file(self, *, experiment_id: str) -> None: ...

    def sandbox_enrichment(
        self, *, row: dict[str, Any], name: str = ""
    ) -> dict[str, Any]: ...

    def push_initial(
        self,
        *,
        session: dict[str, Any],
        name: str = "",
        on_retry: OnPushRetry | None = None,
    ) -> dict[str, Any]: ...

    def sync_pull(
        self, *, session: dict[str, Any], name: str = "", skip_if_busy: bool = False
    ) -> dict[str, Any]: ...

    def final_pull(
        self, *, session: dict[str, Any], name: str = "", deadline: str | None = None
    ) -> dict[str, Any]: ...

    def restore_parachute(
        self, *, experiment_id: str, data: bytes, name: str = ""
    ) -> dict[str, Any]: ...

    def ensure_local_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]: ...

    def merge_local_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]: ...

    def stop_dashboards(self, *, sandbox_id: str = "") -> None: ...

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
    ``SshRsyncSyncer`` (push/pull), ``DashboardTunnels`` (ssh -L pool), and
    ``MetricsArchive``; machine-local sandbox facts (key path, local sync dir,
    loopback dashboard URLs) persist in ``SandboxLocalState``, never in
    cloud-bound rows.
    """

    def __init__(
        self,
        *,
        workspace: LocalWorkspace,
        backend: SandboxBackend,
        rsync_syncer: SshRsyncSyncer | None = None,
    ) -> None:
        self.workspace = workspace
        self.rsync_syncer = rsync_syncer or SshRsyncSyncer()
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
        # One rsync per experiment at a time; sync/release/reap contend.
        # Machine-local serialization only — subordinate to the sync lease,
        # which is the cross-client authority (plan Phase 4).
        self._sync_locks: dict[str, threading.Lock] = {}
        self._sync_locks_lock = threading.Lock()

    # ---------- identity ----------

    def client_id(self) -> str:
        """Stable data-plane client identity — the sync-lease holder id."""
        return self.state.client_id()

    # ---------- workspace ----------

    def ensure_workspace(self, *, experiment_id: str, name: str = "") -> Path:
        """Create the experiment's one local folder (its sandbox sync root).

        Workspace creation happens on the first data-routed touch (plan §3.1);
        record services only return logical folder guidance.
        """
        folder = self.workspace.experiment_dir(experiment_id=experiment_id, name=name)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def local_experiment_dir(self, *, experiment_id: str, name: str = "") -> Path:
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

    def remove_conn_file(self, *, experiment_id: str) -> None:
        self._conn.remove_conn(experiment_id=experiment_id)

    def sandbox_enrichment(
        self, *, row: dict[str, Any], name: str = ""
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
            self._conn.write_command_wrapper(row=row, key_path=key_path) if live else ""
        )
        raw_command = (
            self._conn.raw_ssh_command(row=row, key_path=key_path) if live else ""
        )
        return {
            "key_path": str(key_path),
            "command": command,
            "raw_command": raw_command,
            "local_dir": str(
                self.local_experiment_dir(experiment_id=experiment_id, name=name)
            ),
        }

    # ---------- rsync ----------

    def push_initial(
        self,
        *,
        session: dict[str, Any],
        name: str = "",
        on_retry: OnPushRetry | None = None,
    ) -> dict[str, Any]:
        session = _require_session(session)
        experiment_id = str(session["experiment_id"])
        ssh = session["ssh"]
        local_dir = self.local_experiment_dir(experiment_id=experiment_id, name=name)
        local_dir.mkdir(parents=True, exist_ok=True)
        attempts = max(
            1,
            int(env_float(
                "RESEARCH_PLUGIN_SANDBOX_INITIAL_PUSH_ATTEMPTS",
                None,
                DEFAULT_INITIAL_PUSH_ATTEMPTS,
            )),
        )
        retry_seconds = env_float(
            "RESEARCH_PLUGIN_SANDBOX_INITIAL_PUSH_RETRY",
            None,
            DEFAULT_INITIAL_PUSH_RETRY_SECONDS,
        )
        result: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = self.rsync_syncer.push_initial(
                    ssh_host=str(ssh.get("host") or ""),
                    ssh_port=int(ssh.get("port") or 0),
                    ssh_user=str(ssh.get("user") or "root"),
                    key_path=self.key_path(experiment_id=experiment_id),
                    remote_sync_dir=str(session["remote"]["experiment_dir"]),
                    local_sync_dir=local_dir,
                ).as_dict()
                break
            except Exception:  # noqa: BLE001 — first push races cloud-init; retry briefly
                if attempt >= attempts:
                    raise
                if on_retry is not None:
                    on_retry(attempt, attempts)
                time.sleep(retry_seconds)
        assert result is not None
        self.state.record(experiment_id=experiment_id, local_sync_dir=str(local_dir))
        return result

    def sync_pull(
        self, *, session: dict[str, Any], name: str = "", skip_if_busy: bool = False
    ) -> dict[str, Any]:
        session = _require_session(session)
        experiment_id = str(session["experiment_id"])
        with self._sync_locks_lock:
            lock = self._sync_locks.setdefault(experiment_id, threading.Lock())
        acquired = lock.acquire(blocking=not skip_if_busy)
        if not acquired:
            return {
                "provider": "ssh_rsync",
                "skipped": "busy",
                "pulled": 0,
                "conflicts": 0,
                "local_dir": str(
                    self.local_experiment_dir(experiment_id=experiment_id, name=name)
                ),
            }
        try:
            local_dir = self.local_experiment_dir(
                experiment_id=experiment_id, name=name
            )
            ssh = session["ssh"]
            remote = session["remote"]
            result = self.rsync_syncer.sync(
                ssh_host=str(ssh.get("host") or ""),
                ssh_port=int(ssh.get("port") or 0),
                ssh_user=str(ssh.get("user") or "root"),
                key_path=self.key_path(experiment_id=experiment_id),
                remote_sync_dir=str(remote.get("experiment_dir") or ""),
                local_sync_dir=local_dir,
                # Sandbox-authored telemetry (MLflow db, TB events, transcript)
                # lives outside the experiment folder and lands in a daemon-owned
                # local dir, keyed by sandbox id so each VM generation's history
                # is preserved. Legacy rows simply have nothing at this remote
                # path (their sessions ride inside the synced folder).
                remote_sessions_dir=str(remote.get("sessions_dir") or ""),
                local_sessions_dir=self.workspace.sessions_dir(
                    experiment_id=experiment_id,
                    sandbox_id=str(session.get("sandbox_id") or ""),
                ),
            ).as_dict()
            self.state.record(
                experiment_id=experiment_id, local_sync_dir=str(local_dir)
            )
            return result
        finally:
            lock.release()

    def final_pull(
        self, *, session: dict[str, Any], name: str = "", deadline: str | None = None
    ) -> dict[str, Any]:
        """Last pull before terminate (release + the reaper's final_pull task).

        ``deadline`` is the task's cloud-minted budget, carried but unenforced
        in-process — the local worker is by definition reachable, so this is
        a busy-skipping pull. When the pull fails (or a split-mode daemon
        misses the deadline), the control plane fires the expiry parachute
        over the management channel instead (plan Phase 5, decision 5).
        """
        del deadline
        return self.sync_pull(session=session, name=name, skip_if_busy=True)

    def restore_parachute(
        self, *, experiment_id: str, data: bytes, name: str = ""
    ) -> dict[str, Any]:
        """Unpack a parachute object into the experiment's local folder.

        The tar IS the remote experiment dir at reap time — same excludes and
        size caps as a final pull (shared transfer spec) — so it lands at the
        normal sync target, under the same per-experiment lock the rsync
        paths take. Unlike a pull there is no ``--delete``: a parachute is a
        recovery object, not a mirror pass, so restoring never removes local
        files. The task channel is URL-first so the data plane downloads the
        archive before calling this method; inline bytes remain a compatibility
        path for in-process callers.
        """
        with self._sync_locks_lock:
            lock = self._sync_locks.setdefault(experiment_id, threading.Lock())
        with lock:
            local_dir = self.local_experiment_dir(
                experiment_id=experiment_id, name=name
            )
            local_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                members = tar.getmembers()
                # The 'data' filter refuses absolute paths and parent-dir
                # escapes and strips dangerous modes — a parachute object is
                # remote-produced bytes and gets no more trust than a pull.
                tar.extractall(path=local_dir, filter="data")
            restored = sum(1 for member in members if member.isfile())
            self.state.record(
                experiment_id=experiment_id, local_sync_dir=str(local_dir)
            )
        return {
            "provider": "parachute",
            "restored": restored,
            "local_dir": str(local_dir),
        }

    # ---------- dashboards ----------

    def ensure_local_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return self.dashboards.ensure_local(row=row)

    def merge_local_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return self.dashboards.merged_row(row=row)

    def stop_dashboards(self, *, sandbox_id: str = "") -> None:
        self.dashboards.stop(sandbox_id=sandbox_id)

    # ---------- pulled-metrics fallback ----------

    def pulled_mlflow_db_path(self, *, experiment_id: str, name: str = "") -> Path:
        # The sandbox's MLflow backend store, as mirrored locally by the rsync
        # pull. Current layout: the daemon-owned sessions dir, one subdir per
        # sandbox generation — pick the most recently modified db. Legacy
        # layouts (sessions inside the synced folder) are checked as fallbacks
        # so pre-change experiments keep their lazy metrics backfill.
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
        try:
            live = self.ensure_local_dashboards(row=row)
        except Exception:  # noqa: BLE001 — fall back to the stored URLs
            live = row
        base_url = decode_dashboards(live.get("dashboards_json")).get("mlflow", "")
        snapshot = snapshot_mlflow(base_url) if base_url else None
        if snapshot is not None:
            return snapshot
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
