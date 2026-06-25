"""Background sandbox provisioning: job threads, cancellation, reconcile.

`SandboxProvisioner` owns the acquire lifecycle — idempotent experiment-keyed
jobs for the default path, uid-keyed jobs for additional sandboxes, cooperative
cancellation, orphan cleanup, and the reconcile pass that keeps a polled row
truthful after crashes or restarts.
It talks to persistence through `SandboxRegistry`, applies experiment status
changes only through the workflow engine's system transitions, and reaches
the facade only through ``refresh_row`` (endpoint + dashboard refresh for a
live row).

``connecting``; then a successfully acquired sandbox is recorded as
``running``. The row status stays ``provisioning`` until that handoff, so the
existing cancellation and orphan-cleanup paths (reconcile, release-cancel)
cover every pre-running provider phase.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from ...sandbox.sandbox_backend import (
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    SandboxBackend,
    SandboxRequest,
)
from ...domain.sync_contract import DEFAULT_DATA_DIR, remote_experiment_dir
from ...ports.sandbox_lifecycle import ExperimentTransitions
from ...ports.sandbox_worker import SandboxWorker
from ...utils import iso_after, now_iso
from .sandbox_registry import SandboxRegistry
from ...sandbox.sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    encode_dashboards,
    parse_iso,
)


RefreshRow = Callable[..., dict[str, Any]]


class _Canceled(Exception):
    """Raised inside a provisioning callback to abort acquire on release."""


@dataclass
class _ProvisionJob:
    """A background provisioning thread plus its control signals."""

    thread: threading.Thread
    cancel: threading.Event
    done: threading.Event
    experiment_id: str
    sandbox_uid: str = ""


class SandboxProvisioner:
    """Owns in-flight provisioning jobs and row reconciliation."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        experiments: ExperimentTransitions,
        worker: SandboxWorker,
        refresh_row: RefreshRow,
        stale_provision_seconds: float,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.experiments = experiments
        self.worker = worker
        self._refresh_row = refresh_row
        self.stale_provision_seconds = stale_provision_seconds
        # Default provisioning is keyed by experiment; additional sandboxes by uid.
        self._jobs: dict[str, _ProvisionJob] = {}
        self._jobs_lock = threading.Lock()

    def _job_key(self, *, experiment_id: str, sandbox_uid: str = "") -> str:
        return sandbox_uid or experiment_id

    def _job_for_row(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> _ProvisionJob | None:
        # Default provisioning jobs predate the row uid; additional jobs use it.
        return self._jobs.get(sandbox_uid) or self._jobs.get(experiment_id)

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
        sandbox_uid = str(row.get("sandbox_uid") or "")
        if status in ACTIVE_SANDBOX_STATUSES and row.get("sandbox_id"):
            if self.is_alive(sandbox_id=str(row["sandbox_id"])):
                self.registry.touch_alive(experiment_id=exp, sandbox_uid=sandbox_uid)
                return self._refresh_row(row=self.registry.get_by_uid(sandbox_uid=sandbox_uid))
            self.registry.mark_terminated(experiment_id=exp, sandbox_uid=sandbox_uid)
            self.registry.emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.expired",
                experiment_id=exp,
                payload={"sandbox_id": row.get("sandbox_id", "")},
            )
            return self.registry.get_by_uid(sandbox_uid=sandbox_uid)
        if status == "provisioning":
            with self._jobs_lock:
                job = self._job_for_row(experiment_id=exp, sandbox_uid=sandbox_uid)
                live_job = bool(job and job.thread.is_alive())
            if live_job and not self.provision_too_old(row=row):
                return row  # genuinely in flight — keep polling
            # The job may have JUST settled; re-read before declaring failure.
            fresh = self.registry.get_by_uid(sandbox_uid=sandbox_uid)
            if fresh.get("status") != "provisioning":
                return self.reconcile(row=fresh)
            self.cleanup_orphan(experiment_id=exp, row=fresh)
            self.registry.mark_failed(
                experiment_id=exp,
                error="provisioning interrupted; call sandbox.request again",
                sandbox_uid=sandbox_uid,
            )
            self.registry.emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.failed",
                experiment_id=exp,
                payload={"error": "provisioning interrupted"},
            )
            return self.registry.get_by_uid(sandbox_uid=sandbox_uid)
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
        sandbox_uid: str = "",
        create_new: bool = False,
    ) -> _ProvisionJob:
        """Return the in-flight job for this experiment, or start a fresh one.

        Idempotent: a second request during provisioning attaches to the same
        job rather than starting a duplicate.
        """
        sandbox_uid = str(sandbox_uid or req.sandbox_uid or "").strip()
        if not sandbox_uid:
            sandbox_uid = self.registry.new_sandbox_uid()
        job_key = self._job_key(
            experiment_id=experiment_id,
            sandbox_uid=sandbox_uid if create_new else "",
        )
        with self._jobs_lock:
            job = self._jobs.get(job_key)
            if job is not None and job.thread.is_alive():
                return job
        # No live job. Clear any prior/orphan sandbox before a fresh provision so
        # the deterministic Modal name cannot collide (the wedge we hit). Done
        # outside the lock — it may make a network call.
        if not create_new:
            self.cleanup_orphan(experiment_id=experiment_id, row=existing)
        with self._jobs_lock:
            job = self._jobs.get(job_key)
            if job is not None and job.thread.is_alive():
                return job
            self.begin_provisioning_row(
                experiment_id=experiment_id,
                project_id=project_id,
                req=req,
                sandbox_uid=sandbox_uid,
                create_new=create_new,
            )
            cancel = threading.Event()
            done = threading.Event()
            thread = threading.Thread(
                target=self._provision,
                args=(experiment_id, project_id, req, cancel, done, sandbox_uid),
                name=f"provision-{sandbox_uid or experiment_id}",
                daemon=True,
            )
            job = _ProvisionJob(
                thread=thread,
                cancel=cancel,
                done=done,
                experiment_id=experiment_id,
                sandbox_uid=sandbox_uid,
            )
            self._jobs[job_key] = job
            thread.start()
            return job

    def cancel(self, *, experiment_id: str, sandbox_uid: str | None = None) -> None:
        """Signal in-flight job(s) for the experiment to abort."""
        target_uid = (sandbox_uid or "").strip()
        with self._jobs_lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.experiment_id == experiment_id
                and (not target_uid or job.sandbox_uid == target_uid)
            ]
        for job in jobs:
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
        sandbox_uid: str = "",
    ) -> None:
        """Background worker: create → tunnel, updating the row per phase."""
        try:
            def on_phase(phase: str, detail: str) -> None:
                if cancel.is_set():
                    raise _Canceled()
                self.set_provision(
                    experiment_id=experiment_id,
                    sandbox_uid=sandbox_uid,
                    phase=phase,
                    detail=detail,
                )

            def on_created(sandbox_id: str, sandbox_name: str) -> None:
                # Persist the id the moment it exists so a crash/restart can
                # reconcile or clean it up — this is the orphan fix.
                self.set_provision(
                    experiment_id=experiment_id,
                    sandbox_uid=sandbox_uid,
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
                self._settle_canceled(
                    experiment_id=experiment_id,
                    project_id=project_id,
                    sandbox_uid=sandbox_uid,
                )
                return
            name = self.registry.experiment_name(experiment_id=experiment_id)
            if cancel.is_set():
                self._terminate_quietly(sandbox_id=provisioned.sandbox_id)
                self._settle_canceled(
                    experiment_id=experiment_id,
                    project_id=project_id,
                    sandbox_uid=sandbox_uid,
                )
                return
            now = now_iso()
            self.registry.upsert(
                experiment_id=experiment_id,
                sandbox_uid=sandbox_uid,
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
                },
            )
        except _Canceled:
            # acquire already terminated anything it created.
            self._settle_canceled(
                experiment_id=experiment_id,
                project_id=project_id,
                sandbox_uid=sandbox_uid,
            )
        except (BackendUnavailableError, BackendValidationError, BackendPermissionError) as exc:
            self._settle_failed(
                experiment_id=experiment_id,
                project_id=project_id,
                error=str(exc),
                sandbox_uid=sandbox_uid,
            )
        except Exception as exc:  # noqa: BLE001 — never lose the row to an unexpected error
            self._settle_failed(
                experiment_id=experiment_id,
                project_id=project_id,
                error=str(exc),
                sandbox_uid=sandbox_uid,
            )
        finally:
            done.set()
            job_key = self._job_key(experiment_id=experiment_id, sandbox_uid=sandbox_uid)
            with self._jobs_lock:
                current = self._jobs.get(job_key)
                if current is not None and current.done is done:
                    self._jobs.pop(job_key, None)

    def begin_provisioning_row(
        self,
        *,
        experiment_id: str,
        project_id: str,
        req: SandboxRequest,
        sandbox_uid: str = "",
        create_new: bool = False,
    ) -> None:
        now = now_iso()
        sandbox_uid = str(req.sandbox_uid or sandbox_uid or "").strip()
        if not sandbox_uid:
            sandbox_uid = self.registry.new_sandbox_uid()
        writer = self.registry.create_sandbox if create_new else self.registry.upsert
        writer(
            experiment_id=experiment_id,
            sandbox_uid=sandbox_uid,
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
            # Control knows a management keypair exists for this sandbox (plan
            # Phase 5): a store reference only — never key material.
            mgmt_key_ref=(str(req.sandbox_uid or sandbox_uid) if req.management_public_key else ""),
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
        sandbox_uid: str = "",
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
        self.registry.upsert(
            experiment_id=experiment_id, sandbox_uid=sandbox_uid, **fields
        )

    def reap_stale_provisions(
        self, *, now: datetime, deadline_seconds: float
    ) -> int:
        """Terminate billing VMs left by provisions that wedged past the deadline.

        A provisioning row is written *before* ``acquire`` runs, and the provider
        VM can exist from the ``creating`` phase onward (Lambda launches the
        instance there; Modal creates the sandbox just before ``on_created``). If
        the job dies — daemon crash/restart, OOM, host reboot — the row stays
        ``provisioning`` with a live, billing VM behind it. ``cleanup_orphan``
        reaps it whether or not the id was recorded (deterministic-name lookup),
        but nothing *triggers* that without the agent happening to re-poll. This
        is that trigger: any provisioning row older than ``deadline_seconds``
        whose job is not alive in this process is wedged by definition (a healthy
        provision flips to ``running`` in a couple of minutes) and gets reaped.

        Two independent guards keep it from killing a healthy in-flight provision:
        the in-process live-job check (covers local mode, where the job runs
        here — Lambda's 5-15 min cold boot must not be reaped from under itself)
        and the wall-clock deadline (covers the control plane, which cannot see
        the data-plane job thread). Idempotent and best-effort per row; returns
        how many were reaped.
        """
        reaped = 0
        for row in self.registry.list_rows_by_status(status="provisioning"):
            experiment_id = str(row.get("experiment_id") or "")
            sandbox_uid = str(row.get("sandbox_uid") or "")
            with self._jobs_lock:
                job = self._job_for_row(
                    experiment_id=experiment_id, sandbox_uid=sandbox_uid
                )
                if job is not None and job.thread.is_alive():
                    continue  # a live job in this process still owns it
            started = parse_iso(row.get("provision_started_at"))
            if started is None or (now - started).total_seconds() < deadline_seconds:
                continue
            # The job may have JUST settled the row between the list and here;
            # only reap one that is still provisioning.
            fresh = self.registry.get_by_uid(sandbox_uid=sandbox_uid)
            if fresh.get("status") != "provisioning":
                continue
            try:
                self.cleanup_orphan(experiment_id=experiment_id, row=fresh)
                self.registry.mark_failed(
                    experiment_id=experiment_id,
                    sandbox_uid=sandbox_uid,
                    error=(
                        "provisioning wedged past deadline (daemon offline?); "
                        "the sandbox was terminated — call sandbox.request again"
                    ),
                )
                self.registry.emit_event(
                    project_id=str(fresh.get("project_id") or ""),
                    event_type="sandbox.failed",
                    experiment_id=experiment_id,
                    payload={
                        "error": "stale provision reaped",
                        "phase": fresh.get("phase", ""),
                        "sandbox_id": fresh.get("sandbox_id", ""),
                    },
                )
                reaped += 1
            except Exception:  # noqa: BLE001 — one bad row never aborts the pass
                continue
        return reaped

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
            self.worker.stop_mlflow_access(sandbox_id=str(sid))
            self._terminate_quietly(sandbox_id=str(sid))
        if not sid:
            sandbox_uid = str((row or {}).get("sandbox_uid") or "")
            active_sibling = bool(
                sandbox_uid
                and self.registry.has_active_for_experiment(
                    experiment_id=experiment_id, exclude_sandbox_uid=sandbox_uid
                )
            )
            lookup_uids: list[str] = []
            if sandbox_uid:
                lookup_uids.append(sandbox_uid)
            # Avoid the experiment alias while a sibling is live; it names the primary.
            if not active_sibling:
                lookup_uids.append("")
            if not lookup_uids:
                lookup_uids.append("")
            orphan = None
            for lookup_uid in lookup_uids:
                if orphan:
                    break
                try:
                    orphan = self.backend.find_sandbox_id(
                        experiment_id=experiment_id, sandbox_uid=lookup_uid
                    )
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

    def _settle_canceled(
        self, *, experiment_id: str, project_id: str, sandbox_uid: str = ""
    ) -> None:
        self.registry.mark_terminated(
            experiment_id=experiment_id, sandbox_uid=sandbox_uid
        )
        self.registry.emit_event(
            project_id=project_id,
            event_type="sandbox.released",
            experiment_id=experiment_id,
            payload={"canceled": True},
        )

    def _settle_failed(
        self,
        *,
        experiment_id: str,
        project_id: str,
        error: str,
        sandbox_uid: str = "",
    ) -> None:
        self.registry.mark_failed(
            experiment_id=experiment_id,
            error=error,
            sandbox_uid=sandbox_uid,
        )
        self.registry.emit_event(
            project_id=project_id,
            event_type="sandbox.failed",
            experiment_id=experiment_id,
            payload={"error": error},
        )
