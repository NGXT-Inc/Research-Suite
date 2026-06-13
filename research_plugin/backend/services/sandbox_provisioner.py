"""Background sandbox provisioning: job threads, cancellation, reconcile.

`SandboxProvisioner` owns the acquire lifecycle — one background job per
experiment (idempotent attach), cooperative cancellation, orphan cleanup, and
the reconcile pass that keeps a polled row truthful after crashes or restarts.
It talks to persistence through `SandboxRegistry`, applies experiment status
changes only through the workflow engine's system transitions, and reaches
the facade only through ``refresh_row`` (endpoint + dashboard refresh for a
live row).

The provision job's row phases: provider ``acquire`` reports ``creating`` /
``connecting``; then the row sits in the explicit ``awaiting_initial_push``
phase (cloud plan Phase 4) while the lease-authorized ``initial_push`` task
delivers the experiment folder, and only a confirmed push flips the row to
``running``. The row status stays ``provisioning`` throughout, so the
existing cancellation and orphan-cleanup paths (reconcile, release-cancel)
cover the new phase — a daemon offline mid-push must never leave a billing VM
with no files unaccounted for.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    # Typing-only: runtime imports here would close the package cycle
    # services → dataplane → services (metrics_archive) and break
    # `import backend.dataplane` as an entry point.
    from ..dataplane.tasks import InProcessTaskChannel
    from ..dataplane.worker import DataPlaneWorker

from ..execution import (
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    SandboxBackend,
    SandboxRequest,
)
from ..execution.sync_dirs import DEFAULT_DATA_DIR, remote_experiment_dir
from ..utils import now_iso
from .experiments import ExperimentService
from .sandbox_registry import SandboxRegistry
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    _Canceled,
    _ProvisionJob,
    encode_dashboards,
    iso_after,
    parse_iso,
)
from .sync_sessions import SyncSessionService


RefreshRow = Callable[..., dict[str, Any]]


class SandboxProvisioner:
    """Owns in-flight provisioning jobs and row reconciliation."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        experiments: ExperimentService,
        worker: DataPlaneWorker,
        sessions: SyncSessionService,
        tasks: InProcessTaskChannel,
        refresh_row: RefreshRow,
        stale_provision_seconds: float,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.experiments = experiments
        self.worker = worker
        self.sessions = sessions
        self.tasks = tasks
        self._refresh_row = refresh_row
        self.stale_provision_seconds = stale_provision_seconds
        # In-flight provisioning jobs, keyed by experiment_id.
        self._jobs: dict[str, _ProvisionJob] = {}
        self._jobs_lock = threading.Lock()

    # ---------- liveness ----------

    def is_alive(self, *, sandbox_id: str) -> bool:
        try:
            return bool(self.backend.is_alive(sandbox_id=sandbox_id))
        except Exception:  # noqa: BLE001
            return False

    def reconcile(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Bring a row in line with reality. Read-only-safe (never provisions).

        - running → confirm liveness; mark terminated if the sandbox is gone.
        - provisioning → if a live job in this process owns it (and it is not
          stale), leave it for the agent to keep polling; otherwise the job is
          gone (daemon restart) or wedged, so clean up any orphan and mark
          failed. This is what guarantees a polling agent always reaches a
          terminal state.
        """
        status = row.get("status")
        exp = str(row.get("experiment_id"))
        if status in ACTIVE_SANDBOX_STATUSES and row.get("sandbox_id"):
            if self.is_alive(sandbox_id=str(row["sandbox_id"])):
                self.registry.touch_alive(experiment_id=exp)
                return self._refresh_row(row=self.registry.load_row(experiment_id=exp))
            self.registry.mark_terminated(experiment_id=exp)
            self.registry.emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.expired",
                experiment_id=exp,
                payload={"sandbox_id": row.get("sandbox_id", "")},
            )
            return self.registry.load_row(experiment_id=exp)
        if status == "provisioning":
            with self._jobs_lock:
                job = self._jobs.get(exp)
                live_job = bool(job and job.thread.is_alive())
            if live_job and not self.provision_too_old(row=row):
                return row  # genuinely in flight — keep polling
            # The job may have JUST settled; re-read before declaring failure.
            fresh = self.registry.load_row(experiment_id=exp)
            if fresh.get("status") != "provisioning":
                return self.reconcile(row=fresh)
            self.cleanup_orphan(experiment_id=exp, row=fresh)
            self.registry.mark_failed(
                experiment_id=exp,
                error="provisioning interrupted; call sandbox.request again",
            )
            self.registry.emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.failed",
                experiment_id=exp,
                payload={"error": "provisioning interrupted"},
            )
            return self.registry.load_row(experiment_id=exp)
        return row

    def provision_too_old(self, *, row: dict[str, Any]) -> bool:
        started = parse_iso(row.get("provision_started_at"))
        if started is None:
            return False
        return (
            datetime.now(tz=UTC) - started
        ).total_seconds() > self.stale_provision_seconds

    # ---------- jobs ----------

    def ensure_job(
        self,
        *,
        experiment_id: str,
        project_id: str,
        req: SandboxRequest,
        existing: dict[str, Any] | None,
    ) -> _ProvisionJob:
        """Return the in-flight job for this experiment, or start a fresh one.

        Idempotent: a second request during provisioning attaches to the same
        job rather than starting a duplicate.
        """
        with self._jobs_lock:
            job = self._jobs.get(experiment_id)
            if job is not None and job.thread.is_alive():
                return job
        # No live job. Clear any prior/orphan sandbox before a fresh provision so
        # the deterministic Modal name cannot collide (the wedge we hit). Done
        # outside the lock — it may make a network call.
        self.cleanup_orphan(experiment_id=experiment_id, row=existing)
        with self._jobs_lock:
            job = self._jobs.get(experiment_id)
            if job is not None and job.thread.is_alive():
                return job
            self.begin_provisioning_row(
                experiment_id=experiment_id, project_id=project_id, req=req
            )
            cancel = threading.Event()
            done = threading.Event()
            thread = threading.Thread(
                target=self._provision,
                args=(experiment_id, project_id, req, cancel, done),
                name=f"provision-{experiment_id}",
                daemon=True,
            )
            job = _ProvisionJob(thread=thread, cancel=cancel, done=done)
            self._jobs[experiment_id] = job
            thread.start()
            return job

    def cancel(self, *, experiment_id: str) -> None:
        """Signal the experiment's in-flight job (if any) to abort."""
        with self._jobs_lock:
            job = self._jobs.get(experiment_id)
        if job is not None:
            job.cancel.set()

    def shutdown(self) -> None:
        """Signal all in-flight provisioning jobs to stop (best-effort)."""
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()
        for job in jobs:
            try:
                job.thread.join(timeout=2.0)
            except RuntimeError:
                pass

    def _provision(
        self,
        experiment_id: str,
        project_id: str,
        req: SandboxRequest,
        cancel: threading.Event,
        done: threading.Event,
    ) -> None:
        """Background worker: sync → create → tunnel, updating the row per phase."""
        try:
            def on_phase(phase: str, detail: str) -> None:
                if cancel.is_set():
                    raise _Canceled()
                self.set_provision(experiment_id=experiment_id, phase=phase, detail=detail)

            def on_created(sandbox_id: str, sandbox_name: str) -> None:
                # Persist the id the moment it exists so a crash/restart can
                # reconcile or clean it up — this is the orphan fix.
                self.set_provision(
                    experiment_id=experiment_id,
                    sandbox_id=sandbox_id,
                    sandbox_name=sandbox_name,
                )
                if cancel.is_set():
                    raise _Canceled()

            provisioned = self.backend.acquire(
                request=req, on_phase=on_phase, on_created=on_created
            )
            # Release may have arrived during the final, uninterruptible tunnel
            # wait (cancel isn't checked there). Honor it now rather than marking
            # a just-terminated sandbox `running`.
            if cancel.is_set():
                self._terminate_quietly(sandbox_id=provisioned.sandbox_id)
                self._settle_canceled(experiment_id=experiment_id, project_id=project_id)
                return
            # The explicit phase between provider-acquire and running (plan
            # Phase 4): the VM exists but its experiment folder hasn't been
            # confirmed yet. Status stays `provisioning`, so cancellation
            # (this on_phase checks the cancel flag) and reconcile/orphan
            # cleanup cover it like any other in-flight phase.
            on_phase("awaiting_initial_push", "pushing the local experiment folder")
            name = self.registry.experiment_name(experiment_id=experiment_id)
            try:
                # The push is lease-authorized and session-shaped: the control
                # plane grants the lease and mints the transfer contract, then
                # enqueues an initial_push task — in split mode the daemon's
                # task loop executes it; in-process it runs synchronously.
                session = self.sessions.grant(
                    experiment_id=experiment_id,
                    sandbox_id=provisioned.sandbox_id,
                    ssh_host=provisioned.ssh_host,
                    ssh_port=provisioned.ssh_port,
                    ssh_user=provisioned.ssh_user,
                    experiment_dir=provisioned.sync_dir
                    or provisioned.workdir
                    or remote_experiment_dir(experiment_id=experiment_id, name=name),
                    data_dir=provisioned.sandbox_data_dir or provisioned.unsynced_dir,
                )
                initial_sync = self.tasks.submit(
                    task_type="initial_push",
                    payload={
                        "session": session,
                        "name": name,
                        "on_retry": lambda attempt, attempts: self.set_provision(
                            experiment_id=experiment_id,
                            phase="awaiting_initial_push",
                            detail=f"waiting for remote workspace (attempt {attempt}/{attempts})",
                        ),
                    },
                )
            except Exception:
                self._terminate_quietly(sandbox_id=provisioned.sandbox_id)
                raise
            self.registry.emit_event(
                project_id=project_id,
                event_type="sandbox.initial_rsynchronized",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": provisioned.sandbox_id,
                    "pushed": initial_sync.get("pulled", 0),
                    # Logical (repo-relative) spelling: event payloads are
                    # cloud-bound rows and must not carry absolute local paths.
                    "local_dir": self.worker.repo_relative(
                        initial_sync.get("local_dir", "")
                    ),
                    "remote_dir": initial_sync.get("remote_dir", ""),
                    "duration_seconds": initial_sync.get("duration_seconds", 0),
                },
            )
            if cancel.is_set():
                self._terminate_quietly(sandbox_id=provisioned.sandbox_id)
                self._settle_canceled(experiment_id=experiment_id, project_id=project_id)
                return
            now = now_iso()
            self.registry.upsert(
                experiment_id=experiment_id,
                project_id=project_id,
                status="running",
                sandbox_id=provisioned.sandbox_id,
                # Record what the backend actually procured so the UI/metrics
                # frame the real reserved hardware (Lambda resolves these from
                # the chosen SKU; Modal leaves them empty and we keep req's).
                gpu=provisioned.gpu or (req.gpu or ""),
                cpu=provisioned.cpu if provisioned.cpu is not None else req.cpu,
                memory=provisioned.memory if provisioned.memory is not None else int(req.memory),
                instance_type=provisioned.instance_type or (req.instance_type or ""),
                region=provisioned.region or (req.region or ""),
                # Cost governance (cloud plan Phase 7): record the provider's
                # price quote on the row, and append a per-generation ledger row
                # below so spend survives the row's per-experiment overwrite.
                price_usd_per_hour=provisioned.price_usd_per_hour,
                ssh_host=provisioned.ssh_host,
                ssh_port=provisioned.ssh_port,
                ssh_user=provisioned.ssh_user,
                workdir=provisioned.workdir,
                sync_dir=provisioned.sync_dir or provisioned.workdir,
                unsynced_dir=provisioned.unsynced_dir or provisioned.sandbox_data_dir,
                sandbox_data_dir=provisioned.sandbox_data_dir,
                initial_pushed=int(initial_sync.get("pulled", -1)),
                volume_name=provisioned.volume_name,
                dashboards_json=encode_dashboards(provisioned.dashboards),
                expires_at=iso_after(seconds=req.time_limit),
                last_seen_at=now,
                phase="",
                detail="",
                error="",
                terminated_at="",
            )
            # Per-generation spend ledger (cloud plan Phase 7): one row per
            # provisioned generation so the price survives the row's
            # per-experiment overwrite. Best-effort — a ledger write must never
            # fail an otherwise-successful provision.
            try:
                self.registry.record_generation(
                    experiment_id=experiment_id,
                    project_id=project_id,
                    sandbox_id=provisioned.sandbox_id,
                    instance_type=provisioned.instance_type or (req.instance_type or ""),
                    gpu=provisioned.gpu or (req.gpu or ""),
                    price_usd_per_hour=provisioned.price_usd_per_hour,
                )
            except Exception:  # noqa: BLE001
                pass
            self.experiments.apply_system_transition(
                experiment_id=experiment_id,
                transition="sandbox_started",
            )
            self.registry.emit_event(
                project_id=project_id,
                event_type="sandbox.created",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": provisioned.sandbox_id,
                    "gpu": provisioned.gpu or req.gpu or "",
                    "instance_type": provisioned.instance_type or (req.instance_type or ""),
                    "region": provisioned.region or (req.region or ""),
                    "time_limit": req.time_limit,
                    "initial_sync": {
                        "provider": initial_sync.get("provider", "ssh_rsync"),
                        "direction": initial_sync.get("direction", "push"),
                        "pushed": initial_sync.get("pulled", 0),
                        "local_dir": initial_sync.get("local_dir", ""),
                        "remote_dir": initial_sync.get("remote_dir", ""),
                    },
                },
            )
        except _Canceled:
            # acquire already terminated anything it created.
            self._settle_canceled(experiment_id=experiment_id, project_id=project_id)
        except (BackendUnavailableError, BackendValidationError, BackendPermissionError) as exc:
            self._settle_failed(
                experiment_id=experiment_id, project_id=project_id, error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 — never lose the row to an unexpected error
            self._settle_failed(
                experiment_id=experiment_id, project_id=project_id, error=str(exc)
            )
        finally:
            done.set()
            with self._jobs_lock:
                current = self._jobs.get(experiment_id)
                if current is not None and current.done is done:
                    self._jobs.pop(experiment_id, None)

    def begin_provisioning_row(
        self, *, experiment_id: str, project_id: str, req: SandboxRequest
    ) -> None:
        now = now_iso()
        self.registry.upsert(
            experiment_id=experiment_id,
            project_id=project_id,
            status="provisioning",
            phase="starting",
            detail="",
            error="",
            sandbox_id="",
            sandbox_name="",
            ssh_host="",
            ssh_port=0,
            ssh_user="root",
            workdir=req.remote_workdir or remote_experiment_dir(experiment_id=experiment_id),
            sync_dir=req.remote_workdir or remote_experiment_dir(experiment_id=experiment_id),
            unsynced_dir=DEFAULT_DATA_DIR,
            initial_pushed=-1,
            # Control knows a management keypair exists for this sandbox (plan
            # Phase 5): a store reference only — never key material.
            mgmt_key_ref=(experiment_id if req.management_public_key else ""),
            gpu=req.gpu or "",
            cpu=req.cpu,
            memory=req.memory,
            instance_type=req.instance_type or "",
            region=req.region or "",
            time_limit=req.time_limit,
            requested_at=now,
            provision_started_at=now,
            expires_at="",
            last_seen_at=now,
            terminated_at="",
        )

    def set_provision(
        self,
        *,
        experiment_id: str,
        phase: str | None = None,
        detail: str | None = None,
        sandbox_id: str | None = None,
        sandbox_name: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {"status": "provisioning"}
        if phase is not None:
            fields["phase"] = phase
        if detail is not None:
            fields["detail"] = detail
        if sandbox_id is not None:
            fields["sandbox_id"] = sandbox_id
        if sandbox_name is not None:
            fields["sandbox_name"] = sandbox_name
        self.registry.upsert(experiment_id=experiment_id, **fields)

    def cleanup_orphan(self, *, experiment_id: str, row: dict[str, Any] | None) -> None:
        """Best-effort terminate any sandbox tied to this experiment.

        Covers both a recorded sandbox_id (from a prior/failed row) and the
        deterministic-named orphan a dead job may have left on the backend.
        Stops the recorded id's dashboard tunnels too — this path runs before
        re-provisioning, where no terminal row mark (and so no registry hook)
        would otherwise tear them down.
        """
        seen: set[str] = set()
        sid = (row or {}).get("sandbox_id")
        if sid:
            seen.add(str(sid))
            self.worker.stop_dashboards(sandbox_id=str(sid))
            self._terminate_quietly(sandbox_id=str(sid))
        try:
            orphan = self.backend.find_sandbox_id(experiment_id=experiment_id)
        except Exception:  # noqa: BLE001
            orphan = None
        if orphan and str(orphan) not in seen:
            self._terminate_quietly(sandbox_id=str(orphan))

    # ---------- settle helpers ----------

    def _terminate_quietly(self, *, sandbox_id: str) -> None:
        try:
            self.backend.terminate(sandbox_id=sandbox_id)
        except Exception:  # noqa: BLE001
            pass

    def _settle_canceled(self, *, experiment_id: str, project_id: str) -> None:
        self.registry.mark_terminated(experiment_id=experiment_id)
        self.registry.emit_event(
            project_id=project_id,
            event_type="sandbox.released",
            experiment_id=experiment_id,
            payload={"canceled": True},
        )

    def _settle_failed(self, *, experiment_id: str, project_id: str, error: str) -> None:
        self.registry.mark_failed(experiment_id=experiment_id, error=error)
        self.registry.emit_event(
            project_id=project_id,
            event_type="sandbox.failed",
            experiment_id=experiment_id,
            payload={"error": error},
        )
