"""Central sandbox registry facade.

`SandboxService` is the single authority for sandbox procurement, status, and
shutdown. Policy it owns:

  - **One sandbox per experiment.** The `sandboxes` table is keyed by
    experiment_id; a request upserts that row.
  - **Reuse-if-alive.** A request reuses the experiment's existing sandbox when
    the backend still reports it alive; otherwise it creates a fresh one.
  - **Per-experiment SSH keypair.** The registry generates and owns an ed25519
    keypair per experiment, authorizes the public key in the sandbox, and hands
    the agent a ready-to-run `ssh` command.

The agent never submits commands here. It calls `request` to get SSH details,
then runs commands itself over SSH. Visibility comes from the in-sandbox
transcript, surfaced through `terminal`.

This module holds only the public verbs (request/get/sync/release/terminal,
metrics, views glue). The machinery lives in dedicated collaborators:
  - `sandbox_registry.SandboxRegistry` — every sandboxes-table read/write,
    status marks, and the sandbox event stream.
  - `sandbox_provisioner.SandboxProvisioner` — background provisioning jobs,
    cancellation, orphan cleanup, and row reconciliation.
  - `sandbox_dashboards.DashboardTunnels` — the ssh -L tunnel pool, provider
    dashboard-URL refresh, and MLflow deep links.
  - `sandbox_daemons.SandboxDaemons` — the auto-rsync poller and the
    expiration reaper threads.
  - `sandbox_support` — constants, pure helpers, the SSH dispatcher template.
  - `sandbox_conn.SandboxConnFiles` — SSH key + dispatcher + conn-file plumbing.
  - `sandbox_views` — row→response projections (agent view, row view, etc.).

Experiment status never changes here or in the collaborators except through
the workflow engine's system transitions (see services/workflow_gates.py).
Presentation belongs to the caller: the service returns the agent view and raw
rows; the HTTP layer shapes the UI responses from `get_row`/`rows`/
`sample_metrics`/`backend_health`.
"""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path
from typing import Any

from ..execution.ssh_rsync import SshRsyncSyncer
from ..execution.sync_dirs import DEFAULT_SYNC_DIR
from ..state.activity import ActivityLogger
from ..state.store import StateStore, row_to_dict
from ..utils import NotFoundError, PermissionDeniedError, ValidationError, now_iso
from ..execution import (
    BackendUnavailableError,
    ProvisionedSandbox,
    SandboxBackend,
    SandboxRequest,
)
from . import sandbox_views
from .experiments import ExperimentService
from .metrics_archive import MetricsArchive, snapshot_mlflow, snapshot_mlflow_db
from .sandbox_conn import SandboxConnFiles
from .sandbox_daemons import SandboxDaemons
from .sandbox_dashboards import DashboardTunnels
from .sandbox_provisioner import SandboxProvisioner
from .sandbox_registry import SandboxRegistry
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    DEFAULT_INITIAL_PUSH_ATTEMPTS,
    DEFAULT_INITIAL_PUSH_RETRY_SECONDS,
    DEFAULT_REQUEST_WAIT_SECONDS,
    DEFAULT_STALE_PROVISION_SECONDS,
    METRICS_CACHE_TTL_SECONDS,
    METRICS_PERSIST_TTL_SECONDS,
    decode_dashboards,
    env_float,
    parse_terminal_markers,
    validate_request_inputs,
)


class SandboxService:
    """Facade over sandbox persistence, provisioning, dashboards, and daemons."""

    def __init__(
        self,
        *,
        store: StateStore,
        sandbox_backend: SandboxBackend,
        activity: ActivityLogger | None = None,
        request_wait_seconds: float | None = None,
        stale_provision_seconds: float | None = None,
        rsync_syncer: SshRsyncSyncer | None = None,
        experiments: ExperimentService | None = None,
    ) -> None:
        self.store = store
        self.backend = sandbox_backend
        self.activity = activity
        # Sandbox lifecycle changes experiment status only through the workflow
        # engine's system transitions — never by writing the experiments table.
        self.experiments = experiments or ExperimentService(store=store)
        self.keys_dir = store.repo_root / ".research_plugin" / "sandboxes" / "keys"
        self._conn = SandboxConnFiles(repo_root=store.repo_root, keys_dir=self.keys_dir)
        self.rsync_syncer = rsync_syncer or SshRsyncSyncer()
        self.request_wait_seconds = env_float(
            "RESEARCH_PLUGIN_SANDBOX_REQUEST_WAIT",
            request_wait_seconds,
            DEFAULT_REQUEST_WAIT_SECONDS,
        )
        # Short-TTL cache of live-usage samples, keyed by sandbox_id.
        self._metrics_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._metrics_lock = threading.Lock()
        # Durable per-experiment metrics snapshots (results outlive the VM).
        self.metrics_archive = MetricsArchive(repo_root=store.repo_root)
        self._metrics_persisted_at: dict[str, float] = {}
        self._sync_locks: dict[str, threading.Lock] = {}
        self._sync_locks_lock = threading.Lock()

        self.registry = SandboxRegistry(store=store)
        self.dashboards = DashboardTunnels(
            registry=self.registry,
            backend=sandbox_backend,
            key_path=self._key_path,
        )
        # Marking a row failed/terminated also tears down its runtime
        # attachments; the registry stays persistence-only via this hook.
        self.registry.on_terminal = self._on_terminal_row
        self.provisioner = SandboxProvisioner(
            registry=self.registry,
            backend=sandbox_backend,
            experiments=self.experiments,
            key_path=self._key_path,
            push_initial=self._push_initial_files,
            refresh_row=self._refresh_row,
            stop_tunnels=self.dashboards.stop,
            stale_provision_seconds=env_float(
                "RESEARCH_PLUGIN_SANDBOX_STALE",
                stale_provision_seconds,
                DEFAULT_STALE_PROVISION_SECONDS,
            ),
        )
        self.daemons = SandboxDaemons(
            registry=self.registry,
            backend=sandbox_backend,
            provisioner=self.provisioner,
            experiments=self.experiments,
            sync_row=self._sync_row,
            persist_metrics=self._persist_metrics_row,
        )
        self.daemons.start()

    # ---------- agent / tool surface ----------

    def request(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        gpu: str | None = None,
        cpu: float | None = None,
        memory: int | None = None,
        time_limit: int | None = None,
        instance_type: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        caps = self.backend.capabilities
        gpu, cpu, memory, time_limit = validate_request_inputs(
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            configurable_resources=caps.configurable_resources,
        )
        instance_type = (instance_type or "").strip() or None
        region = (region or "").strip() or None

        # Resolve scope + experiment gate, and read any existing row, in one txn.
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            experiment = conn.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if experiment is None or experiment["project_id"] != project_id:
                raise NotFoundError(
                    f"experiment not found in project {project_id}: {experiment_id}"
                )
            if experiment["status"] not in {"ready_to_run", "running"}:
                raise PermissionDeniedError(
                    "sandbox.request requires experiment status ready_to_run or running"
                )
            existing = row_to_dict(
                row=conn.execute(
                    "SELECT * FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
                ).fetchone()
            )

        public_key, key_path = self._ensure_keypair(experiment_id=experiment_id)

        # 1) Reuse a live sandbox immediately — the common mid-session case.
        if (
            existing
            and existing.get("status") in ACTIVE_SANDBOX_STATUSES
            and existing.get("sandbox_id")
            and self.provisioner.is_alive(sandbox_id=str(existing["sandbox_id"]))
        ):
            self.registry.touch_alive(experiment_id=experiment_id)
            self._mark_experiment_running(experiment_id=experiment_id)
            row = self._refresh_row(row=self.registry.load_row(experiment_id=experiment_id))
            row = self.dashboards.ensure_local(row=row)
            self.registry.emit_event(
                project_id=project_id,
                event_type="sandbox.reused",
                experiment_id=experiment_id,
                payload={"sandbox_id": existing["sandbox_id"]},
            )
            return self._agent_view(row=row, key_path=key_path, reused=True)

        # 2) Hardware-selection gate. A provider that bundles GPU + CPU + RAM into
        #    fixed machine types (Lambda Labs) has nothing sensible to default to,
        #    and provisioning the wrong one costs real money. With no live sandbox
        #    to reuse and no instance_type chosen yet, hand the agent the current
        #    availability menu and let it pick — exactly the "here's what we have,
        #    pick one" flow. Configurable backends (Modal) skip this entirely.
        if caps.requires_hardware_selection and not instance_type:
            return self._needs_selection_view(
                experiment_id=experiment_id,
                project_id=project_id,
                gpu=gpu,
                region=region,
            )

        # 3) Otherwise (re)start provisioning in the background and best-effort
        #    wait up to the budget. A big first sync or a cold GPU returns
        #    `provisioning` (the agent polls sandbox.get); a fast one returns SSH
        #    inline, exactly like before. Backend errors are handled inside the
        #    job, which lands the row in `failed` — so request never times out.
        req = SandboxRequest(
            experiment_id=experiment_id,
            project_id=project_id,
            public_key=public_key,
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            instance_type=instance_type,
            region=region,
        )
        job = self.provisioner.ensure_job(
            experiment_id=experiment_id,
            project_id=project_id,
            req=req,
            existing=existing,
        )
        job.done.wait(timeout=self.request_wait_seconds)
        row = self.registry.load_row(experiment_id=experiment_id)
        row = self.dashboards.ensure_local(row=row)
        reused = False if row.get("status") == "running" else None
        return self._agent_view(row=row, key_path=key_path, reused=reused)

    def get(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Read-only poll target. Never provisions; reconciles stale state."""
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id, project_id=project_id
            )
        except NotFoundError:
            # Soften only the genuine "never provisioned" case so the poll loop
            # never has to catch an exception. A project-scope mismatch (the row
            # exists under another project) is a real error and still raises.
            if self.registry.exists(experiment_id=experiment_id):
                raise
            return {
                "experiment_id": experiment_id,
                "status": "none",
                "hint": "No sandbox for this experiment — call sandbox.request to create one.",
            }
        row = self.dashboards.ensure_local(row=self.provisioner.reconcile(row=row))
        key_path = self._key_path(experiment_id=experiment_id)
        return self._agent_view(row=row, key_path=key_path, reused=None)

    def options(
        self,
        *,
        project_id: str | None = None,  # noqa: ARG002 — scope handled by router
        gpu: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        """Describe the hardware the agent can request from the active backend.

        Lambda Labs returns a live, cheapest-first menu of available machine
        SKUs (the agent passes one back as ``sandbox.request(instance_type=...)``).
        Modal returns its static gpu/cpu/memory menu. Read-only; never provisions.
        """
        caps = self.backend.capabilities
        catalog = self._hardware_catalog(gpu=gpu, region=region)
        selection_required = bool(caps.requires_hardware_selection)
        hint = (
            "Pick one options[].instance_type and call "
            "sandbox.request(experiment_id, instance_type=..., region=?). "
            "Options are sorted cheapest-first and reflect live capacity."
            if selection_required
            else (
                "Call sandbox.request(experiment_id, gpu=?, cpu=?, memory=?). "
                "Omit gpu for a CPU-only sandbox."
            )
        )
        return {"backend": caps.name, **catalog, "hint": hint}

    def list_sandboxes(self, *, project_id: str | None = None) -> dict[str, Any]:
        return {
            "sandboxes": [
                self._agent_summary(row=row)
                for row in self.registry.list_rows(project_id=project_id)
            ]
        }

    def release(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any]:
        row = self.registry.fetch_scoped(experiment_id=experiment_id, project_id=project_id)
        # Signal any in-flight provisioning job to abort. It terminates whatever
        # it created via the cancel path (acquire's cleanup), even if create is
        # still mid-flight when we return.
        self.provisioner.cancel(experiment_id=experiment_id)
        stopped = False
        if row.get("sandbox_id") and row.get("status") in ACTIVE_SANDBOX_STATUSES:
            try:
                self._sync_row(row=row, skip_if_busy=True)
            except Exception:  # noqa: BLE001 — release should still terminate
                pass
            # Last chance to read MLflow: the server dies with the VM.
            self._persist_metrics_row(row=row, force=True)
        if row.get("sandbox_id") and row.get("status") in (ACTIVE_SANDBOX_STATUSES | {"provisioning"}):
            try:
                stopped = self.backend.terminate(sandbox_id=str(row["sandbox_id"]))
            except Exception:  # noqa: BLE001
                stopped = False
        # Belt-and-suspenders: clear any named orphan we may have created.
        self.provisioner.cleanup_orphan(experiment_id=experiment_id, row=row)
        self.registry.mark_terminated(experiment_id=experiment_id)
        self.registry.emit_event(
            project_id=str(row["project_id"]),
            event_type="sandbox.released",
            experiment_id=experiment_id,
            payload={"sandbox_id": row.get("sandbox_id", ""), "stopped": stopped},
        )
        return self._row_view(row=self.registry.load_row(experiment_id=experiment_id))

    def terminal(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        tail: int | None = None,
        since: int | None = None,
    ) -> dict[str, Any]:
        """Read the experiment's terminal transcript.

        Supports incremental polling: every response carries a ``cursor`` (the
        end offset of the full transcript). Pass it back as ``since=`` to receive
        only the output produced since — instead of re-pulling the whole tail on
        every poll. ``running`` tells the agent whether the sandbox is still
        alive, so it can stop polling a finished/terminated one.

        It also surfaces per-command structure parsed from the rec.sh transcript
        markers: ``last_exit_code`` / ``last_command_finished_at`` (the most
        recent finished command's status + timestamp) and ``command_running``
        (a command is still in flight). These let an agent tell when a command
        finished and whether it succeeded without re-scanning the tail. They are
        best-effort and null on sandboxes created before the markers landed.

        (Reading the full transcript to compute an accurate cursor is a daemon-
        side cost; a future backend ``transcript_length`` could avoid it. The
        agent-facing payload is what ``since`` keeps small.)
        """
        row = self.registry.fetch_scoped(experiment_id=experiment_id, project_id=project_id)
        status = str(row.get("status", "none"))
        full = ""
        unavailable = False
        try:
            full = self.backend.read_transcript(
                sandbox_id=str(row.get("sandbox_id") or ""),
                experiment_id=experiment_id,
                volume_name=str(row.get("volume_name") or ""),
                workdir=str(row.get("workdir") or ""),
                tail=None,
                # Stored endpoint + per-experiment key, for backends that read
                # the transcript over plain SSH (Lambda Labs).
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                ssh_user=str(row.get("ssh_user") or ""),
                key_path=str(row.get("key_path") or self._key_path(experiment_id=experiment_id)),
            )
        except Exception as exc:  # noqa: BLE001
            full = f"(terminal unavailable: {exc})"
            unavailable = True
        cursor = len(full)
        if unavailable:
            transcript = full
        elif since is not None:
            transcript = full[min(int(since), cursor):]
        elif tail is not None and tail >= 0 and cursor > tail:
            transcript = full[-tail:]
        else:
            transcript = full
        # Parse exit markers from the FULL transcript (the last one may predate
        # the `since` cursor) and gate "command running" on the sandbox actually
        # being alive — a dead sandbox isn't running a command, even if its log
        # ends on a command-start marker with no recorded exit.
        if unavailable:
            last_exit_code: int | None = None
            last_command_finished_at: str | None = None
            command_running: bool | None = None
        else:
            last_exit_code, last_command_finished_at, in_flight = parse_terminal_markers(full)
            command_running = in_flight and status in ACTIVE_SANDBOX_STATUSES
        return {
            "experiment_id": experiment_id,
            "sandbox_id": row.get("sandbox_id", ""),
            "status": status,
            "running": status in ACTIVE_SANDBOX_STATUSES,
            "transcript": transcript,
            "cursor": cursor,
            "new_chars": len(transcript) if since is not None else None,
            "last_exit_code": last_exit_code,
            "last_command_finished_at": last_command_finished_at,
            "command_running": command_running,
        }

    def sync(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any]:
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id, project_id=project_id
            )
        except NotFoundError as exc:
            raise ValidationError(
                "sandbox.sync requires a running sandbox; call sandbox.request first"
            ) from exc
        row = self.provisioner.reconcile(row=row)
        if row.get("status") not in ACTIVE_SANDBOX_STATUSES or not row.get("sandbox_id"):
            raise ValidationError(
                "sandbox.sync requires a running sandbox; call sandbox.request first"
            )
        try:
            result = self._sync_row(row=row)
        except BackendUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(f"sandbox sync failed: {exc}") from exc
        self._persist_metrics_row(row=row, force=True)
        self.registry.emit_event(
            project_id=str(row["project_id"]),
            event_type="sandbox.synced",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": row.get("sandbox_id", ""),
                "pulled": result.get("pulled", 0),
                "conflicts": result.get("conflicts", 0),
                "local_dir": result.get("local_dir", ""),
            },
        )
        has_conflicts = bool(result.get("conflicts") or result.get("skipped_conflicts"))
        hint = (
            "Sandbox files were pulled with rsync, but the "
            "local sync has conflicts. Resolve the reported conflict paths, then "
            "run sandbox.sync again before registering or associating resources."
            if has_conflicts
            else (
                "Sandbox files under the remote sync directory have been rsynced "
                "to the local experiment folder. Now register/associate local result "
                "files with resource.register_file and resource.associate before "
                "sandbox.release."
            )
        )
        return {
            "experiment_id": experiment_id,
            "project_id": row.get("project_id"),
            "sandbox_id": row.get("sandbox_id"),
            "status": row.get("status"),
            "workdir": row.get("workdir"),
            "sync_dir": row.get("sync_dir") or row.get("workdir"),
            "unsynced_dir": row.get("unsynced_dir") or row.get("sandbox_data_dir") or "",
            "local_sync_dir": self._local_sync_dir(experiment_id=experiment_id),
            "sandbox_data_dir": row.get("sandbox_data_dir") or "",
            "sync": result,
            "hint": hint,
        }

    def results_metrics(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Archived MLflow metrics for an experiment.

        Served from the daemon-owned archive, so it works long after the
        sandbox is terminated. ``available=False`` means nothing was ever
        captured (no MLflow runs existed, or the sandbox predates archiving).
        """
        status = "none"
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id, project_id=project_id
            )
            status = str(row.get("status") or "none")
        except NotFoundError:
            if self.registry.exists(experiment_id=experiment_id):
                raise  # exists under another project — a real scope error
        data = self.metrics_archive.load(experiment_id=experiment_id)
        if data is None:
            # Lazy backfill: the MLflow backend store lives inside the synced
            # workspace, so the rsync pull usually captured mlflow.db even for
            # sandboxes that died before REST archiving existed.
            snapshot = snapshot_mlflow_db(self._pulled_mlflow_db_path(experiment_id=experiment_id))
            if snapshot is not None:
                with contextlib.suppress(OSError):
                    self.metrics_archive.persist(experiment_id=experiment_id, snapshot=snapshot)
                data = self.metrics_archive.load(experiment_id=experiment_id)
        if data is None:
            return {
                "experiment_id": experiment_id,
                "available": False,
                "sandbox_status": status,
                "hint": (
                    "No archived metrics yet — they are captured from the "
                    "sandbox's MLflow on sync and right before release."
                ),
            }
        return {
            "experiment_id": experiment_id,
            "available": True,
            "sandbox_status": status,
            **data,
        }

    def _persist_metrics_row(self, *, row: dict[str, Any], force: bool = False) -> None:
        """Best-effort: archive the sandbox's MLflow metrics on the daemon's disk.

        The MLflow server dies with the VM; this snapshot is what makes results
        outlive it. Called throttled from the auto-sync loop and forced on
        explicit sync / release / reap (before terminate). Never raises, and
        never overwrites an existing archive with emptiness — an unreachable
        tunnel at release time just keeps the last good snapshot.
        """
        try:
            experiment_id = str(row.get("experiment_id") or "")
            if not experiment_id:
                return
            now = time.monotonic()
            last = self._metrics_persisted_at.get(experiment_id)
            if not force and last is not None and now - last < METRICS_PERSIST_TTL_SECONDS:
                return
            try:
                live = self.dashboards.ensure_local(row=row)
            except Exception:  # noqa: BLE001 — fall back to the stored URLs
                live = row
            base_url = decode_dashboards(live.get("dashboards_json")).get("mlflow", "")
            snapshot = snapshot_mlflow(base_url) if base_url else None
            if snapshot is None:
                # REST unreachable (tunnel died, server crashed): fall back to
                # the mlflow.db the rsync pull just brought down.
                snapshot = snapshot_mlflow_db(
                    self._pulled_mlflow_db_path(experiment_id=experiment_id)
                )
            if snapshot is None:
                return
            self._metrics_persisted_at[experiment_id] = now
            path = self.metrics_archive.persist(
                experiment_id=experiment_id, snapshot=snapshot
            )
            if force:
                self.registry.emit_event(
                    project_id=str(row.get("project_id") or ""),
                    event_type="sandbox.metrics_persisted",
                    experiment_id=experiment_id,
                    payload={
                        "sandbox_id": row.get("sandbox_id", ""),
                        "path": str(path),
                        "runs": sum(
                            len(e.get("runs") or [])
                            for e in snapshot.get("experiments") or []
                        ),
                    },
                )
        except Exception:  # noqa: BLE001 — archiving must never block sync/release
            return

    def health(self) -> dict[str, Any]:
        health = self.backend.health()
        result = {"ok": bool(health.get("ok"))}
        if not result["ok"] and health.get("error"):
            result["error"] = health["error"]
        return result

    # ---------- domain primitives for the HTTP/UI layer ----------
    #
    # The service returns raw rows and sampled data; the HTTP layer shapes the
    # UI responses (see ResearchHttpApi.sandbox_*_view). This keeps presentation
    # out of the domain service.

    def get_row(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any] | None:
        """Reconciled sandbox row for an experiment, or None if none exists."""
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id, project_id=project_id
            )
        except NotFoundError:
            return None
        return self.dashboards.ensure_local(row=self.provisioner.reconcile(row=row))

    def rows(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        """All sandbox rows for a project (most-recent first)."""
        return [
            self.dashboards.ensure_local(row=row)
            for row in self.registry.list_rows(project_id=project_id)
        ]

    def backend_health(self) -> dict[str, Any]:
        """Full backend health payload (the slim ``health`` tool trims this)."""
        return self.backend.health()

    def sample_metrics(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Sample live in-container usage (CPU/RAM/GPU) for a running sandbox.

        Read-only and best-effort: returns ``available: False`` (never raises)
        when there is no sandbox, it is not running, or the sampler came back
        empty (e.g. a CPU-only image without nvidia-smi). The row's *reserved*
        gpu/cpu/memory ride along so the UI can frame used-vs-reserved.
        """
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id, project_id=project_id
            )
        except NotFoundError:
            return {"experiment_id": experiment_id, "status": "none", "available": False, "metrics": None}
        status = row.get("status")
        sandbox_id = str(row.get("sandbox_id") or "")
        base: dict[str, Any] = {
            "experiment_id": experiment_id,
            "sandbox_id": sandbox_id,
            "status": status,
            "reserved": {
                "gpu": row.get("gpu") or "",
                "cpu": row.get("cpu"),
                "memory_mib": row.get("memory"),
                "instance_type": row.get("instance_type") or "",
                "region": row.get("region") or "",
            },
        }
        if status not in ACTIVE_SANDBOX_STATUSES or not sandbox_id:
            return {**base, "available": False, "metrics": None}
        metrics = self._sample_metrics_cached(
            experiment_id=experiment_id, sandbox_id=sandbox_id, row=row
        )
        return {**base, "available": metrics is not None, "metrics": metrics, "sampled_at": now_iso()}

    def _sample_metrics_cached(
        self, *, experiment_id: str, sandbox_id: str, row: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._metrics_lock:
            cached = self._metrics_cache.get(sandbox_id)
            if cached is not None and now - cached[0] < METRICS_CACHE_TTL_SECONDS:
                return cached[1]
        try:
            metrics = self.backend.sample_metrics(
                sandbox_id=sandbox_id,
                # Stored endpoint + per-experiment key, for backends that sample
                # over plain SSH (Lambda Labs). Modal ignores these.
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                ssh_user=str(row.get("ssh_user") or ""),
                key_path=str(row.get("key_path") or self._key_path(experiment_id=experiment_id)),
            )
        except Exception:  # noqa: BLE001 — metrics are best-effort
            metrics = None
        with self._metrics_lock:
            self._metrics_cache[sandbox_id] = (time.monotonic(), metrics)
        return metrics

    # ---------- workflow / home helpers ----------

    def sandboxes_for_experiment(self, *, conn, experiment_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM sandboxes WHERE experiment_id = ? ORDER BY rowid DESC",
            (experiment_id,),
        ).fetchall()
        return [self._row_view(row=row_to_dict(row=row) or {}) for row in rows]

    def sandboxes_for_project(self, *, conn, project_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM sandboxes WHERE project_id = ? ORDER BY rowid DESC",
            (project_id,),
        ).fetchall()
        return [self._row_view(row=row_to_dict(row=row) or {}) for row in rows]

    # ---------- lifecycle plumbing ----------

    def shutdown(self) -> None:
        """Stop the daemons, in-flight provisioning jobs, and tunnels."""
        self.daemons.stop()
        self.provisioner.shutdown()
        self.dashboards.stop()

    def reap_expired(self, **kwargs: Any) -> int:
        """Terminate running sandboxes past expires_at (see SandboxDaemons)."""
        return self.daemons.reap_expired(**kwargs)

    def _mark_experiment_running(self, *, experiment_id: str) -> None:
        """A live sandbox means execution started: ready_to_run → running.

        Routed through the workflow engine's system transitions (so the move
        lands in the experiment.transitioned event log); a no-op when the
        experiment is already running or past it.
        """
        self.experiments.apply_system_transition(
            experiment_id=experiment_id,
            transition="sandbox_started",
        )

    def _on_terminal_row(self, experiment_id: str, sandbox_id: str | None) -> None:
        """Registry terminal hook: tear down a row's runtime attachments.

        ``sandbox_id`` is None when the row itself was missing — skip tunnel
        teardown but still drop the conn file, matching the pre-split behavior.
        """
        if sandbox_id is not None:
            self.dashboards.stop(sandbox_id=sandbox_id)
        self._remove_conn(experiment_id=experiment_id)

    def _refresh_row(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Endpoint + provider-dashboard refresh for a confirmed-live row."""
        return self.dashboards.maybe_refresh(row=self._maybe_refresh_endpoint(row=row))

    def _maybe_refresh_endpoint(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Re-read a live sandbox's SSH tunnel and persist it if it moved.

        Recovers the "sandbox alive ≠ tunnel endpoint still current" case
        (e.g. Modal relocates a sandbox): the new host/port is written back so
        the agent view + conn file hand out a working command.

        Strictly best-effort. A failure here — including a transient *local*
        resolver outage hitting the Modal control plane, the very thing the
        sbx dispatcher's retry/keepalive already absorbs — leaves the stored
        endpoint untouched and never breaks request/get. Only ``running`` rows
        with a sandbox id are probed.
        """
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id or row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return row
        try:
            endpoint = self.backend.refresh_ssh_endpoint(sandbox_id=sandbox_id)
        except Exception:  # noqa: BLE001 — refresh must never break the caller
            endpoint = None
        if not endpoint:
            return row
        host, port = str(endpoint[0] or ""), int(endpoint[1] or 0)
        if not host or not port:
            return row
        if host == str(row.get("ssh_host") or "") and port == int(row.get("ssh_port") or 0):
            return row  # unchanged — the common case; avoid a needless write
        experiment_id = str(row.get("experiment_id"))
        self.registry.upsert(experiment_id=experiment_id, ssh_host=host, ssh_port=port)
        self.registry.emit_event(
            project_id=str(row.get("project_id")),
            event_type="sandbox.endpoint_refreshed",
            experiment_id=experiment_id,
            payload={"ssh_host": host, "ssh_port": port},
        )
        return self.registry.load_row(experiment_id=experiment_id)

    # ---------- sync engine ----------

    def _sync_row(self, *, row: dict[str, Any], skip_if_busy: bool = False) -> dict[str, Any]:
        experiment_id = str(row.get("experiment_id") or "")
        with self._sync_locks_lock:
            lock = self._sync_locks.setdefault(experiment_id, threading.Lock())
        acquired = lock.acquire(blocking=not skip_if_busy)
        if not acquired:
            return {
                "provider": "ssh_rsync",
                "skipped": "busy",
                "pulled": 0,
                "conflicts": 0,
                "local_dir": str(self._local_sync_dir(experiment_id=experiment_id)),
            }
        try:
            local_dir = (
                Path(row.get("local_sync_dir") or "")
                if row.get("local_sync_dir")
                else self._local_sync_dir(experiment_id=experiment_id)
            )
            result = self.rsync_syncer.sync(
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                ssh_user=str(row.get("ssh_user") or "root"),
                key_path=Path(str(row.get("key_path") or self._key_path(experiment_id=experiment_id))),
                remote_sync_dir=str(row.get("sync_dir") or row.get("workdir") or DEFAULT_SYNC_DIR),
                local_sync_dir=local_dir,
            ).as_dict()
            self.registry.emit_event(
                project_id=str(row.get("project_id")),
                event_type="sandbox.rsynced",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": row.get("sandbox_id", ""),
                    "pulled": result.get("pulled", 0),
                    "local_dir": result.get("local_dir", ""),
                    "duration_seconds": result.get("duration_seconds", 0),
                },
            )
            return result
        finally:
            lock.release()

    def _push_initial_files(
        self,
        *,
        experiment_id: str,
        project_id: str,
        provisioned: ProvisionedSandbox,
    ) -> dict[str, Any]:
        local_dir = self._local_sync_dir(experiment_id=experiment_id)
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
                    ssh_host=provisioned.ssh_host,
                    ssh_port=provisioned.ssh_port,
                    ssh_user=provisioned.ssh_user,
                    key_path=self._key_path(experiment_id=experiment_id),
                    remote_sync_dir=provisioned.sync_dir or provisioned.workdir or DEFAULT_SYNC_DIR,
                    local_sync_dir=local_dir,
                ).as_dict()
                break
            except Exception:  # noqa: BLE001 — first push races cloud-init; retry briefly
                if attempt >= attempts:
                    raise
                self.provisioner.set_provision(
                    experiment_id=experiment_id,
                    phase="syncing",
                    detail=f"waiting for remote workspace (attempt {attempt}/{attempts})",
                )
                time.sleep(retry_seconds)
        assert result is not None
        self.registry.emit_event(
            project_id=project_id,
            event_type="sandbox.initial_rsynchronized",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": provisioned.sandbox_id,
                "pushed": result.get("pulled", 0),
                "local_dir": result.get("local_dir", ""),
                "remote_dir": result.get("remote_dir", ""),
                "duration_seconds": result.get("duration_seconds", 0),
            },
        )
        return result

    # ---------- paths / conn-file plumbing (delegated to SandboxConnFiles) ----------

    def _local_sync_dir(self, *, experiment_id: str) -> Path:
        return self.registry.local_sync_dir(experiment_id=experiment_id)

    def _pulled_mlflow_db_path(self, *, experiment_id: str) -> Path:
        # The sandbox's MLflow backend store, as mirrored locally by the rsync
        # pull (the dashboard bootstrap puts it under the synced workspace's
        # .research_plugin_sessions/<experiment_id>/ directory).
        return (
            self._local_sync_dir(experiment_id=experiment_id)
            / ".research_plugin_sessions"
            / experiment_id
            / "mlflow.db"
        )

    def _key_path(self, *, experiment_id: str) -> Path:
        return self._conn.key_path(experiment_id=experiment_id)

    def _ensure_keypair(self, *, experiment_id: str) -> tuple[str, Path]:
        return self._conn.ensure_keypair(experiment_id=experiment_id)

    def _remove_conn(self, *, experiment_id: str) -> None:
        self._conn.remove_conn(experiment_id=experiment_id)

    # ---------- views (delegated to sandbox_views) ----------

    def _agent_view(
        self, *, row: dict[str, Any], key_path: Path, reused: bool | None
    ) -> dict[str, Any]:
        return sandbox_views.agent_view(
            row=row,
            key_path=key_path,
            reused=reused,
            conn_files=self._conn,
            env_info=self._sandbox_environment(),
            repo_root=self.store.repo_root,
        )

    def _agent_summary(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return sandbox_views.agent_summary(row=row)

    def _row_view(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return sandbox_views.sandbox_row_view(row=row, repo_root=self.store.repo_root)

    def _needs_selection_view(
        self,
        *,
        experiment_id: str,
        project_id: str,
        gpu: str | None,
        region: str | None,
    ) -> dict[str, Any]:
        catalog = self._hardware_catalog(gpu=gpu, region=region)
        return sandbox_views.needs_selection_view(
            experiment_id=experiment_id, project_id=project_id, catalog=catalog
        )

    # ---------- backend introspection ----------

    def _hardware_catalog(
        self, *, gpu: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        """Ask the backend what hardware can be requested (best-effort shape).

        Backends without a hardware catalog return None via SandboxBackendBase,
        which yields an empty, non-selecting catalog here.
        """
        catalog = self.backend.hardware_catalog(gpu=gpu, region=region)
        if catalog is None:
            return {
                "provider": self.backend.capabilities.name,
                "selection_required": False,
                "options": [],
                "regions": [],
            }
        return catalog

    def _sandbox_environment(self) -> dict[str, Any]:
        try:
            result = self.backend.sandbox_environment()
        except Exception:  # noqa: BLE001
            return {"available_tokens": [], "notes": []}
        if not isinstance(result, dict):
            return {"available_tokens": [], "notes": []}
        tokens = [
            str(token)
            for token in result.get("available_tokens", [])
            if isinstance(token, str) and token
        ]
        notes = [
            str(note)
            for note in result.get("notes", [])
            if isinstance(note, str) and note
        ]
        return {"available_tokens": tokens, "notes": notes}
