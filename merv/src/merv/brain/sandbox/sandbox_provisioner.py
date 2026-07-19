"""Background sandbox provisioning: job threads and cancellation.

`SandboxProvisioner` owns the acquire mechanics — uid-keyed job threads and
cooperative cancellation. It talks to persistence through `SandboxRegistry`;
every destructive decision (orphan cleanup, terminal marks + teardown) is the
`SandboxLifecycle`'s, which this module calls but never re-implements. The
lifecycle in turn asks back only one thing, through ``job_is_live``: whether a
provisioning row is still owned by a live job thread in this process.

A successfully acquired sandbox is recorded as ``running``. The row status
stays ``provisioning`` until that handoff, so the cancellation and
orphan-cleanup paths (reconcile, release-cancel) cover every pre-running
provider phase.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .sandbox_backend import (
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    SandboxBackend,
    SandboxRequest,
)
from .sandbox_paths import DEFAULT_DATA_DIR, remote_experiment_dir
from ..kernel.utils import iso_after, now_iso
from .sandbox_lifecycle import SandboxLifecycle
from .sandbox_registry import SandboxRegistry
from .sandbox_support import parse_iso


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
    """Owns in-flight provisioning job threads."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        lifecycle: SandboxLifecycle,
        stale_provision_seconds: float,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.lifecycle = lifecycle
        self.stale_provision_seconds = stale_provision_seconds
        self._jobs: dict[str, _ProvisionJob] = {}
        self._jobs_lock = threading.Lock()

    def _job_key(self, *, experiment_id: str, sandbox_uid: str = "") -> str:
        return sandbox_uid or experiment_id

    def _provider_for(self, *, req: SandboxRequest) -> str:
        """Owning backend's canonical name for this request (data-keyed)."""
        try:
            return self.backend.capabilities_for(provider=req.provider).name
        except Exception:  # noqa: BLE001 — row bookkeeping must not fail a provision
            return req.provider or ""

    def _job_for_row(
        self, *, experiment_id: str, sandbox_uid: str = ""
    ) -> _ProvisionJob | None:
        # Default provisioning jobs predate the row uid; additional jobs use it.
        return self._jobs.get(sandbox_uid) or self._jobs.get(experiment_id)

    def job_is_live(self, *, experiment_id: str, sandbox_uid: str = "") -> bool:
        """Whether a live job thread in this process owns the row.

        The lifecycle's job probe: a live job owns its row at ANY age (Lambda
        boots legitimately run past the stale deadline), so reconcile and the
        stale-provision reaper never condemn a row this returns True for.
        """
        with self._jobs_lock:
            job = self._job_for_row(
                experiment_id=experiment_id, sandbox_uid=sandbox_uid
            )
            return bool(job and job.thread.is_alive())

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
        with self._jobs_lock:
            job = self._jobs.get(sandbox_uid)
            if job is not None and job.thread.is_alive():
                return job
        # No live job. Clear any prior/orphan sandbox before a fresh provision so
        # deterministic provider names cannot collide. Done outside the lock — it
        # may make a network call.
        if not create_new:
            self.lifecycle.cleanup_orphan(experiment_id=experiment_id, row=existing)
        with self._jobs_lock:
            job = self._jobs.get(sandbox_uid)
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
            self._jobs[sandbox_uid] = job
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
                self.lifecycle.terminate_quietly(sandbox_id=provisioned.sandbox_id)
                self._settle_canceled(
                    experiment_id=experiment_id,
                    project_id=project_id,
                    sandbox_uid=sandbox_uid,
                )
                return
            if cancel.is_set():
                self.lifecycle.terminate_quietly(sandbox_id=provisioned.sandbox_id)
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
                provider=self._provider_for(req=req),
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
                    provider=self._provider_for(req=req),
                    instance_type=provisioned.instance_type or (req.instance_type or ""),
                    gpu=provisioned.gpu or (req.gpu or ""),
                    price_usd_per_hour=provisioned.price_usd_per_hour,
                )
            except Exception:  # noqa: BLE001
                pass
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
            public_key_source=req.public_key_source,
            gpu=req.gpu or "",
            cpu=req.cpu,
            memory=req.memory,
            provider=self._provider_for(req=req),
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
            if self.job_is_live(
                experiment_id=experiment_id, sandbox_uid=sandbox_uid
            ):
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
                self.lifecycle.cleanup_orphan(experiment_id=experiment_id, row=fresh)
                self.lifecycle.mark_failed(
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

    # ---------- settle helpers ----------

    def _settle_canceled(
        self, *, experiment_id: str, project_id: str, sandbox_uid: str = ""
    ) -> None:
        self.lifecycle.mark_terminated(
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
        self.lifecycle.mark_failed(
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
