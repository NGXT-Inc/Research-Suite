"""Central sandbox registry.

`SandboxService` is the single authority for sandbox procurement, status, and
shutdown. Policy it owns:

  - **One sandbox per experiment.** The `sandboxes` table is keyed by
    experiment_id; a request upserts that row.
  - **Reuse-if-alive.** A request reuses the experiment's existing sandbox when
    Modal still reports it alive; otherwise it creates a fresh one.
  - **Per-experiment SSH keypair.** The registry generates and owns an ed25519
    keypair per experiment, authorizes the public key in the sandbox, and hands
    the agent a ready-to-run `ssh` command.

The agent never submits commands here. It calls `request` to get SSH details,
then runs commands itself over SSH. Visibility comes from the in-sandbox
transcript, surfaced through `terminal`.

This module holds the state machine only. Its collaborators live alongside it:
  - `sandbox_support` — constants, pure helpers, the SSH dispatcher template.
  - `sandbox_conn.SandboxConnFiles` — SSH key + dispatcher + conn-file plumbing.
  - `sandbox_views` — row→response projections (agent view, row view, etc.).

Presentation belongs to the caller: the service returns the agent view and raw
rows; the HTTP layer shapes the UI responses from `get_row`/`rows`/
`sample_metrics`/`backend_health`.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..execution.ssh_rsync import SshRsyncSyncer
from ..execution.sync_dirs import (
    DEFAULT_SYNC_DIR,
    DEFAULT_UNSYNCED_DIR,
    local_experiment_sync_dir,
)
from ..state.activity import ActivityLogger
from ..state.store import StateStore, row_to_dict
from ..utils import NotFoundError, PermissionDeniedError, ValidationError, now_iso
from ..execution import (
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    ProvisionedSandbox,
    SandboxBackend,
    SandboxRequest,
)
from . import sandbox_views
from .sandbox_conn import SandboxConnFiles
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    DEFAULT_AUTO_RSYNC_INTERVAL_SECONDS,
    DEFAULT_INITIAL_PUSH_ATTEMPTS,
    DEFAULT_INITIAL_PUSH_RETRY_SECONDS,
    DEFAULT_REAPER_INTERVAL_SECONDS,
    DEFAULT_REQUEST_WAIT_SECONDS,
    DEFAULT_STALE_PROVISION_SECONDS,
    METRICS_CACHE_TTL_SECONDS,
    _Canceled,
    _ProvisionJob,
    encode_dashboards,
    env_float,
    iso_after,
    parse_iso,
    parse_terminal_markers,
    validate_request_inputs,
)


class SandboxService:
    """Owns sandbox persistence and delegates provisioning to a backend."""

    def __init__(
        self,
        *,
        store: StateStore,
        sandbox_backend: SandboxBackend,
        activity: ActivityLogger | None = None,
        request_wait_seconds: float | None = None,
        stale_provision_seconds: float | None = None,
        rsync_syncer: SshRsyncSyncer | None = None,
    ) -> None:
        self.store = store
        self.backend = sandbox_backend
        self.activity = activity
        self.keys_dir = store.repo_root / ".research_plugin" / "sandboxes" / "keys"
        self._conn = SandboxConnFiles(repo_root=store.repo_root, keys_dir=self.keys_dir)
        self.rsync_syncer = rsync_syncer or SshRsyncSyncer()
        self.request_wait_seconds = env_float(
            "RESEARCH_PLUGIN_SANDBOX_REQUEST_WAIT",
            request_wait_seconds,
            DEFAULT_REQUEST_WAIT_SECONDS,
        )
        self.stale_provision_seconds = env_float(
            "RESEARCH_PLUGIN_SANDBOX_STALE",
            stale_provision_seconds,
            DEFAULT_STALE_PROVISION_SECONDS,
        )
        # In-flight provisioning jobs, keyed by experiment_id.
        self._jobs: dict[str, _ProvisionJob] = {}
        self._jobs_lock = threading.Lock()
        # Short-TTL cache of live-usage samples, keyed by sandbox_id.
        self._metrics_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._metrics_lock = threading.Lock()
        self._sync_locks: dict[str, threading.Lock] = {}
        self._sync_locks_lock = threading.Lock()
        self._auto_sync_stop = threading.Event()
        self._auto_sync_thread: threading.Thread | None = None
        if self._auto_sync_enabled():
            self._auto_sync_thread = threading.Thread(
                target=self._auto_sync_loop,
                name="sandbox-rsync-poller",
                daemon=True,
            )
            self._auto_sync_thread.start()
        self._reaper_stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None
        if self._reaper_enabled():
            self._reaper_thread = threading.Thread(
                target=self._reaper_loop,
                name="sandbox-reaper",
                daemon=True,
            )
            self._reaper_thread.start()

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
            configurable_resources=getattr(caps, "configurable_resources", True),
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
            and self._is_alive(sandbox_id=str(existing["sandbox_id"]))
        ):
            self._touch_alive(experiment_id=experiment_id)
            self._mark_experiment_running(experiment_id=experiment_id, project_id=project_id)
            row = self._maybe_refresh_endpoint(row=self._load_row(experiment_id=experiment_id))
            row = self._maybe_refresh_dashboards(row=row)
            self._emit_event(
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
        if getattr(caps, "requires_hardware_selection", False) and not instance_type:
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
        job = self._ensure_job(
            experiment_id=experiment_id,
            project_id=project_id,
            req=req,
            existing=existing,
        )
        job.done.wait(timeout=self.request_wait_seconds)
        row = self._load_row(experiment_id=experiment_id)
        reused = False if row.get("status") == "running" else None
        return self._agent_view(row=row, key_path=key_path, reused=reused)

    def get(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Read-only poll target. Never provisions; reconciles stale state."""
        try:
            row = self._fetch_scoped(experiment_id=experiment_id, project_id=project_id)
        except NotFoundError:
            # Soften only the genuine "never provisioned" case so the poll loop
            # never has to catch an exception. A project-scope mismatch (the row
            # exists under another project) is a real error and still raises.
            if self._sandbox_exists(experiment_id=experiment_id):
                raise
            return {
                "experiment_id": experiment_id,
                "status": "none",
                "hint": "No sandbox for this experiment — call sandbox.request to create one.",
            }
        row = self._reconcile(row=row)
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
        selection_required = bool(getattr(caps, "requires_hardware_selection", False))
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
        return {"sandboxes": [self._agent_summary(row=row) for row in self._list_rows(project_id=project_id)]}

    def release(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any]:
        row = self._fetch_scoped(experiment_id=experiment_id, project_id=project_id)
        # Signal any in-flight provisioning job to abort. It terminates whatever
        # it created via the cancel path (acquire's cleanup), even if create is
        # still mid-flight when we return.
        with self._jobs_lock:
            job = self._jobs.get(experiment_id)
        if job is not None:
            job.cancel.set()
        stopped = False
        if row.get("sandbox_id") and row.get("status") in ACTIVE_SANDBOX_STATUSES:
            try:
                self._sync_row(row=row, skip_if_busy=True)
            except Exception:  # noqa: BLE001 — release should still terminate
                pass
        if row.get("sandbox_id") and row.get("status") in (ACTIVE_SANDBOX_STATUSES | {"provisioning"}):
            try:
                stopped = self.backend.terminate(sandbox_id=str(row["sandbox_id"]))
            except Exception:  # noqa: BLE001
                stopped = False
        # Belt-and-suspenders: clear any named orphan we may have created.
        self._cleanup_orphan(experiment_id=experiment_id, row=row)
        self._mark_terminated(experiment_id=experiment_id)
        self._emit_event(
            project_id=str(row["project_id"]),
            event_type="sandbox.released",
            experiment_id=experiment_id,
            payload={"sandbox_id": row.get("sandbox_id", ""), "stopped": stopped},
        )
        return self._row_view(row=self._load_row(experiment_id=experiment_id))

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
        row = self._fetch_scoped(experiment_id=experiment_id, project_id=project_id)
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
            row = self._fetch_scoped(experiment_id=experiment_id, project_id=project_id)
        except NotFoundError as exc:
            raise ValidationError(
                "sandbox.sync requires a running sandbox; call sandbox.request first"
            ) from exc
        row = self._reconcile(row=row)
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
        self._emit_event(
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
            row = self._fetch_scoped(experiment_id=experiment_id, project_id=project_id)
        except NotFoundError:
            return None
        return self._reconcile(row=row)

    def rows(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        """All sandbox rows for a project (most-recent first)."""
        return self._list_rows(project_id=project_id)

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
            row = self._fetch_scoped(experiment_id=experiment_id, project_id=project_id)
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
        metrics = self._sample_metrics_cached(sandbox_id=sandbox_id)
        return {**base, "available": metrics is not None, "metrics": metrics, "sampled_at": now_iso()}

    def _sample_metrics_cached(self, *, sandbox_id: str) -> dict[str, Any] | None:
        sampler = getattr(self.backend, "sample_metrics", None)
        if not callable(sampler):
            return None
        now = time.monotonic()
        with self._metrics_lock:
            cached = self._metrics_cache.get(sandbox_id)
            if cached is not None and now - cached[0] < METRICS_CACHE_TTL_SECONDS:
                return cached[1]
        try:
            metrics = sampler(sandbox_id=sandbox_id)
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

    # ---------- liveness / persistence ----------

    def _reconcile(self, *, row: dict[str, Any]) -> dict[str, Any]:
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
            if self._is_alive(sandbox_id=str(row["sandbox_id"])):
                self._touch_alive(experiment_id=exp)
                refreshed = self._maybe_refresh_endpoint(
                    row=self._load_row(experiment_id=exp)
                )
                return self._maybe_refresh_dashboards(row=refreshed)
            self._mark_terminated(experiment_id=exp)
            self._emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.expired",
                experiment_id=exp,
                payload={"sandbox_id": row.get("sandbox_id", "")},
            )
            return self._load_row(experiment_id=exp)
        if status == "provisioning":
            with self._jobs_lock:
                job = self._jobs.get(exp)
                live_job = bool(job and job.thread.is_alive())
            if live_job and not self._provision_too_old(row=row):
                return row  # genuinely in flight — keep polling
            # The job may have JUST settled; re-read before declaring failure.
            fresh = self._load_row(experiment_id=exp)
            if fresh.get("status") != "provisioning":
                return self._reconcile(row=fresh)
            self._cleanup_orphan(experiment_id=exp, row=fresh)
            self._mark_failed(
                experiment_id=exp,
                error="provisioning interrupted; call sandbox.request again",
            )
            self._emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.failed",
                experiment_id=exp,
                payload={"error": "provisioning interrupted"},
            )
            return self._load_row(experiment_id=exp)
        return row

    # ---------- provisioning jobs ----------

    def _ensure_job(
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
        self._cleanup_orphan(experiment_id=experiment_id, row=existing)
        with self._jobs_lock:
            job = self._jobs.get(experiment_id)
            if job is not None and job.thread.is_alive():
                return job
            self._begin_provisioning_row(
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
                self._set_provision(experiment_id=experiment_id, phase=phase, detail=detail)

            def on_created(sandbox_id: str, sandbox_name: str) -> None:
                # Persist the id the moment it exists so a crash/restart can
                # reconcile or clean it up — this is the orphan fix.
                self._set_provision(
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
                try:
                    self.backend.terminate(sandbox_id=provisioned.sandbox_id)
                except Exception:  # noqa: BLE001
                    pass
                self._mark_terminated(experiment_id=experiment_id)
                self._emit_event(
                    project_id=project_id,
                    event_type="sandbox.released",
                    experiment_id=experiment_id,
                    payload={"canceled": True},
                )
                return
            on_phase("syncing", "pushing local experiment files")
            try:
                initial_sync = self._push_initial_files(
                    experiment_id=experiment_id,
                    project_id=project_id,
                    provisioned=provisioned,
                )
            except Exception:
                try:
                    self.backend.terminate(sandbox_id=provisioned.sandbox_id)
                except Exception:  # noqa: BLE001
                    pass
                raise
            if cancel.is_set():
                try:
                    self.backend.terminate(sandbox_id=provisioned.sandbox_id)
                except Exception:  # noqa: BLE001
                    pass
                self._mark_terminated(experiment_id=experiment_id)
                self._emit_event(
                    project_id=project_id,
                    event_type="sandbox.released",
                    experiment_id=experiment_id,
                    payload={"canceled": True},
                )
                return
            now = now_iso()
            self._upsert_sandbox(
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
                ssh_host=provisioned.ssh_host,
                ssh_port=provisioned.ssh_port,
                ssh_user=provisioned.ssh_user,
                workdir=provisioned.workdir,
                sync_dir=provisioned.sync_dir or provisioned.workdir,
                unsynced_dir=provisioned.unsynced_dir or provisioned.sandbox_data_dir,
                local_sync_dir=str(self._local_sync_dir(experiment_id=experiment_id)),
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
            self._mark_experiment_running(experiment_id=experiment_id, project_id=project_id)
            self._emit_event(
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
            self._mark_terminated(experiment_id=experiment_id)
            self._emit_event(
                project_id=project_id,
                event_type="sandbox.released",
                experiment_id=experiment_id,
                payload={"canceled": True},
            )
        except (BackendUnavailableError, BackendValidationError, BackendPermissionError) as exc:
            self._mark_failed(experiment_id=experiment_id, error=str(exc))
            self._emit_event(
                project_id=project_id,
                event_type="sandbox.failed",
                experiment_id=experiment_id,
                payload={"error": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 — never lose the row to an unexpected error
            self._mark_failed(experiment_id=experiment_id, error=str(exc))
            self._emit_event(
                project_id=project_id,
                event_type="sandbox.failed",
                experiment_id=experiment_id,
                payload={"error": str(exc)},
            )
        finally:
            done.set()
            with self._jobs_lock:
                current = self._jobs.get(experiment_id)
                if current is not None and current.done is done:
                    self._jobs.pop(experiment_id, None)

    def _begin_provisioning_row(
        self, *, experiment_id: str, project_id: str, req: SandboxRequest
    ) -> None:
        now = now_iso()
        self._upsert_sandbox(
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
            workdir=DEFAULT_SYNC_DIR,
            sync_dir=DEFAULT_SYNC_DIR,
            unsynced_dir=DEFAULT_UNSYNCED_DIR,
            local_sync_dir=str(self._local_sync_dir(experiment_id=experiment_id)),
            gpu=req.gpu or "",
            cpu=req.cpu,
            memory=req.memory,
            instance_type=req.instance_type or "",
            region=req.region or "",
            time_limit=req.time_limit,
            key_path=str(self._key_path(experiment_id=experiment_id)),
            requested_at=now,
            provision_started_at=now,
            expires_at="",
            last_seen_at=now,
            terminated_at="",
        )

    def _set_provision(
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
        self._upsert_sandbox(experiment_id=experiment_id, **fields)

    def _cleanup_orphan(self, *, experiment_id: str, row: dict[str, Any] | None) -> None:
        """Best-effort terminate any sandbox tied to this experiment.

        Covers both a recorded sandbox_id (from a prior/failed row) and the
        deterministic-named orphan a dead job may have left on the backend.
        """
        seen: set[str] = set()
        sid = (row or {}).get("sandbox_id")
        if sid:
            seen.add(str(sid))
            try:
                self.backend.terminate(sandbox_id=str(sid))
            except Exception:  # noqa: BLE001
                pass
        finder = getattr(self.backend, "find_sandbox_id", None)
        if callable(finder):
            try:
                orphan = finder(experiment_id=experiment_id)
            except Exception:  # noqa: BLE001
                orphan = None
            if orphan and str(orphan) not in seen:
                try:
                    self.backend.terminate(sandbox_id=str(orphan))
                except Exception:  # noqa: BLE001
                    pass

    def _mark_failed(self, *, experiment_id: str, error: str) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE sandboxes
                SET status = 'failed', error = ?, phase = '', detail = '',
                    terminated_at = ?, updated_at = ?
                WHERE experiment_id = ?
                """,
                (error, now, now, experiment_id),
            )
        self._remove_conn(experiment_id=experiment_id)

    def _provision_too_old(self, *, row: dict[str, Any]) -> bool:
        started = parse_iso(row.get("provision_started_at"))
        if started is None:
            return False
        return (datetime.now(tz=UTC) - started).total_seconds() > self.stale_provision_seconds

    def shutdown(self) -> None:
        """Signal all in-flight provisioning jobs to stop (best-effort)."""
        self._auto_sync_stop.set()
        self._reaper_stop.set()
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()
        for job in jobs:
            try:
                job.thread.join(timeout=2.0)
            except RuntimeError:
                pass
        if self._auto_sync_thread is not None:
            self._auto_sync_thread.join(timeout=2.0)
        if self._reaper_thread is not None:
            self._reaper_thread.join(timeout=2.0)

    # ---------- expiration reaper ----------

    def _reaper_enabled(self) -> bool:
        raw = os.environ.get("RESEARCH_PLUGIN_SANDBOX_REAPER", "1").lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        backend_name = getattr(getattr(self.backend, "capabilities", None), "name", "")
        return backend_name in {"modal", "lambda_labs"}

    def _reaper_loop(self) -> None:
        interval = env_float(
            "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL",
            None,
            DEFAULT_REAPER_INTERVAL_SECONDS,
        )
        while not self._reaper_stop.wait(interval):
            try:
                self.reap_expired()
            except Exception:  # noqa: BLE001 — the reaper must never die
                pass

    def reap_expired(self, *, now: datetime | None = None) -> int:
        """Terminate every running sandbox whose expires_at deadline has passed.

        Idempotent and safe to call directly (tests do). Returns how many were
        reaped. A best-effort final sync runs first so results aren't lost.
        """
        now_dt = now or datetime.now(tz=UTC)
        reaped = 0
        for row in self._list_running_rows():
            expires_at = parse_iso(row.get("expires_at"))
            if expires_at is None or now_dt < expires_at:
                continue
            self._reap_row(row=row)
            reaped += 1
        return reaped

    def _reap_row(self, *, row: dict[str, Any]) -> None:
        experiment_id = str(row.get("experiment_id") or "")
        # Preserve outputs before the kill — same courtesy as release().
        try:
            self._sync_row(row=row, skip_if_busy=True)
        except Exception:  # noqa: BLE001 — reaping must still terminate
            pass
        sandbox_id = str(row.get("sandbox_id") or "")
        stopped = False
        if sandbox_id:
            try:
                stopped = self.backend.terminate(sandbox_id=sandbox_id)
            except Exception:  # noqa: BLE001
                stopped = False
        self._cleanup_orphan(experiment_id=experiment_id, row=row)
        self._mark_terminated(experiment_id=experiment_id)
        reverted = self._revert_running_experiment(experiment_id=experiment_id)
        self._emit_event(
            project_id=str(row.get("project_id")),
            event_type="sandbox.expired",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": sandbox_id,
                "reaped": True,
                "expires_at": row.get("expires_at"),
                "stopped": stopped,
                "experiment_reverted": reverted,
            },
        )

    def _auto_sync_enabled(self) -> bool:
        raw = os.environ.get("RESEARCH_PLUGIN_SANDBOX_AUTO_RSYNC", "1").lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        backend_name = getattr(getattr(self.backend, "capabilities", None), "name", "")
        return backend_name in {"modal", "lambda_labs"}

    def _auto_sync_loop(self) -> None:
        interval = env_float(
            "RESEARCH_PLUGIN_SANDBOX_RSYNC_INTERVAL",
            None,
            DEFAULT_AUTO_RSYNC_INTERVAL_SECONDS,
        )
        # Remember the last error emitted per experiment so a persistent
        # failure (e.g. an unusable local rsync) is reported once instead of
        # flooding the events table every interval. Only this single auto-sync
        # thread touches the dict, so no lock is needed.
        last_errors: dict[str, str] = {}
        while not self._auto_sync_stop.wait(interval):
            rows = self._list_running_rows()
            active = {str(row.get("experiment_id")) for row in rows}
            for gone in set(last_errors) - active:
                last_errors.pop(gone, None)
            for row in rows:
                experiment_id = str(row.get("experiment_id"))
                try:
                    result = self._sync_row(row=row, skip_if_busy=True)
                    if not result.get("skipped"):
                        last_errors.pop(experiment_id, None)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    if last_errors.get(experiment_id) == message:
                        continue  # already reported this exact failure
                    last_errors[experiment_id] = message
                    self._emit_event(
                        project_id=str(row.get("project_id")),
                        event_type="sandbox.rsync_error",
                        experiment_id=experiment_id,
                        payload={"error": message, "sandbox_id": row.get("sandbox_id", "")},
                    )

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
            self._emit_event(
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
                self._set_provision(
                    experiment_id=experiment_id,
                    phase="syncing",
                    detail=f"waiting for remote workspace (attempt {attempt}/{attempts})",
                )
                time.sleep(retry_seconds)
        assert result is not None
        self._emit_event(
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

    def _is_alive(self, *, sandbox_id: str) -> bool:
        try:
            return bool(self.backend.is_alive(sandbox_id=sandbox_id))
        except Exception:  # noqa: BLE001
            return False

    def _maybe_refresh_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Re-read the encrypted dashboard tunnel URLs and persist if changed.

        Companion to ``_maybe_refresh_endpoint``: when a sandbox's tunnels move
        on the Modal side, the SSH host/port AND the dashboard HTTPS URLs all
        change together. Best-effort: a backend without ``dashboard_urls`` or
        an error reading them leaves the stored value untouched.
        """
        refresher = getattr(self.backend, "dashboard_urls", None)
        if not callable(refresher):
            return row
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id or row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return row
        try:
            fresh = refresher(sandbox_id=sandbox_id)
        except Exception:  # noqa: BLE001 — refresh must never break the caller
            return row
        if not isinstance(fresh, dict):
            return row
        normalized = {str(k): str(v) for k, v in fresh.items() if isinstance(v, str) and v}
        encoded = encode_dashboards(normalized)
        if encoded == (row.get("dashboards_json") or "{}"):
            return row
        experiment_id = str(row.get("experiment_id"))
        self._upsert_sandbox(experiment_id=experiment_id, dashboards_json=encoded)
        return self._load_row(experiment_id=experiment_id)

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
        refresher = getattr(self.backend, "refresh_ssh_endpoint", None)
        if not callable(refresher):
            return row
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id or row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return row
        try:
            endpoint = refresher(sandbox_id=sandbox_id)
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
        self._upsert_sandbox(experiment_id=experiment_id, ssh_host=host, ssh_port=port)
        self._emit_event(
            project_id=str(row.get("project_id")),
            event_type="sandbox.endpoint_refreshed",
            experiment_id=experiment_id,
            payload={"ssh_host": host, "ssh_port": port},
        )
        return self._load_row(experiment_id=experiment_id)

    def _upsert_sandbox(self, *, experiment_id: str, **fields: Any) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            payload = dict(fields)
            payload["updated_at"] = now
            if exists is None:
                payload["experiment_id"] = experiment_id
                payload.setdefault("created_at", now)
                columns = ", ".join(payload)
                placeholders = ", ".join("?" for _ in payload)
                conn.execute(
                    f"INSERT INTO sandboxes ({columns}) VALUES ({placeholders})",
                    list(payload.values()),
                )
            else:
                assignments = ", ".join(f"{key} = ?" for key in payload)
                conn.execute(
                    f"UPDATE sandboxes SET {assignments} WHERE experiment_id = ?",
                    [*payload.values(), experiment_id],
                )

    def _touch_alive(self, *, experiment_id: str) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET last_seen_at = ?, updated_at = ? WHERE experiment_id = ?",
                (now, now, experiment_id),
            )

    def _mark_terminated(self, *, experiment_id: str) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE sandboxes
                SET status = 'terminated', terminated_at = ?, updated_at = ?
                WHERE experiment_id = ?
                """,
                (now, now, experiment_id),
            )
        self._remove_conn(experiment_id=experiment_id)

    def _mark_experiment_running(self, *, experiment_id: str, project_id: str) -> None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if row is None or row["status"] != "ready_to_run":
                return
            conn.execute(
                "UPDATE experiments SET status = 'running', updated_at = ? WHERE id = ?",
                (now_iso(), experiment_id),
            )

    def _revert_running_experiment(self, *, experiment_id: str) -> bool:
        """Inverse of _mark_experiment_running, for a sandbox reaped at expiry.

        Without this, an experiment whose sandbox expired underneath it stays
        'running' forever. ready_to_run is truthful (nothing is executing) and
        lets the agent simply request a fresh sandbox. Experiments already
        past running (review or terminal statuses) are left alone — that work
        no longer depends on the sandbox.
        """
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                return False
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run', updated_at = ? WHERE id = ?",
                (now_iso(), experiment_id),
            )
            return True

    def _load_row(self, *, experiment_id: str) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"sandbox not found: {experiment_id}")
            return row_to_dict(row=row) or {}
        finally:
            conn.close()

    def _fetch_scoped(self, *, experiment_id: str, project_id: str | None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            if project_id is not None:
                project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"no sandbox for experiment: {experiment_id}")
            if project_id is not None and row["project_id"] != project_id:
                raise NotFoundError(
                    f"sandbox not found in project {project_id}: {experiment_id}"
                )
            return row_to_dict(row=row) or {}
        finally:
            conn.close()

    def _sandbox_exists(self, *, experiment_id: str) -> bool:
        conn = self.store.connect()
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM sandboxes WHERE experiment_id = ?", (experiment_id,)
                ).fetchone()
                is not None
            )
        finally:
            conn.close()

    def _list_rows(self, *, project_id: str | None) -> list[dict[str, Any]]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE project_id = ? ORDER BY rowid DESC",
                (project_id,),
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]
        finally:
            conn.close()

    def _list_running_rows(self) -> list[dict[str, Any]]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE status = 'running' ORDER BY rowid DESC"
            ).fetchall()
            return [row_to_dict(row=row) or {} for row in rows]
        finally:
            conn.close()

    def _local_sync_dir(self, *, experiment_id: str) -> Path:
        return local_experiment_sync_dir(repo_root=self.store.repo_root, experiment_id=experiment_id)

    def _emit_event(
        self, *, project_id: str, event_type: str, experiment_id: str, payload: dict[str, Any]
    ) -> None:
        with self.store.transaction() as conn:
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type=event_type,
                target_type="sandbox",
                target_id=experiment_id,
                payload=payload,
            )

    # ---------- SSH key / conn-file plumbing (delegated to SandboxConnFiles) ----------

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

        Backends opt in with a ``hardware_catalog`` method; one that doesn't
        (the fake test backend) yields an empty, non-selecting catalog.
        """
        describe = getattr(self.backend, "hardware_catalog", None)
        if not callable(describe):
            return {
                "provider": getattr(self.backend.capabilities, "name", ""),
                "selection_required": False,
                "options": [],
                "regions": [],
            }
        return describe(gpu=gpu, region=region)

    def _sandbox_environment(self) -> dict[str, Any]:
        describe = getattr(self.backend, "sandbox_environment", None)
        if not callable(describe):
            return {"available_tokens": [], "notes": []}
        try:
            result = describe()
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
