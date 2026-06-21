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
  - **Per-sandbox management keypair** (plan Phase 5, fixed decision 4). A
    second, control-plane-owned ed25519 keypair is authorized at bootstrap
    alongside the user key; transcript reads, metrics sampling, and the expiry
    parachute ride it, so none of them depend on the user's machine. The user
    key is data-plane-only (rsync, sbx dispatcher, tunnels).

The agent never submits commands here. It calls `request` to get SSH details,
then runs commands itself over SSH. Visibility comes from the in-sandbox
transcript, surfaced through `terminal`.

This module holds only the public verbs (request/get/sync/release/terminal,
metrics, views glue). The machinery lives in dedicated collaborators:
  - `sandbox_registry.SandboxRegistry` — every sandboxes-table read/write,
    status marks, and the sandbox event stream.
  - `sandbox_provisioner.SandboxProvisioner` — background provisioning jobs,
    cancellation, orphan cleanup, and row reconciliation.
  - `SandboxWorker` — every data-plane duty: SSH keys + conn files, rsync
    push/pull tasks, ssh -L dashboard tunnels, and the pulled-mlflow.db
    metrics fallback (cloud plan §3.1).
  - `sync_sessions` — sync leases (the cross-client byte-movement authority),
    session issuance, and the poller's ControlPlaneView (cloud plan Phase 4).
  - `TaskChannel` — the control→data task seam: initial push, final pull,
    conn refresh, and teardown ride it as tasks.
  - `sandbox_daemons.SandboxDaemons` — the auto-rsync poller and the
    expiration reaper threads.
  - `sandbox_metrics.SandboxMetrics` — metrics archive/read/sample policy.
  - `sandbox_parachute.SandboxParachute` — expiry parachute rescue/restore.
  - `sandbox_support` — constants, pure helpers, the SSH dispatcher template.
  - `sandbox_views` — row→response projections (agent view, row view, etc.).

Experiment status never changes here or in the collaborators except through
the workflow engine's system transitions (see domain/workflow_gates.py).
Presentation belongs to the caller: the service returns the agent view and raw
rows; the HTTP layer shapes the UI responses from `get_row`/`rows`/
`sample_metrics`/`backend_health`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.quota_contract import AdmissionRequest
from ..domain.sync_contract import remote_experiment_dir

from ..state.activity import ActivityLogger
from ..state.blobs import BlobStore
from ..state.store import BaseStateStore, Connection, row_to_dict
from ..utils import (
    NotFoundError,
    PermissionDeniedError,
    ResearchPluginError,
    ValidationError,
)
from ..sandbox_backend import (
    BackendUnavailableError,
    SandboxBackend,
    SandboxRequest,
)
from ..ports.metrics_archive import MetricsArchive
from ..ports.mgmt_keys import MgmtKeyStore
from ..ports.quota_admission import QuotaAdmission
from ..ports.sandbox_lifecycle import ExperimentTransitions
from ..ports.sandbox_worker import SandboxWorker
from ..ports.task_channel import TaskChannel
from . import sandbox_views
from .sandbox_metrics import SandboxMetrics
from .sandbox_parachute import SandboxParachute
from .transcript_cache import TranscriptCache
from .sandbox_daemons import SandboxDaemons
from .sandbox_provisioner import SandboxProvisioner
from .sandbox_registry import SandboxRegistry
from ..sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    DEFAULT_REQUEST_WAIT_SECONDS,
    DEFAULT_STALE_PROVISION_SECONDS,
    encode_dashboards,
    iso_after,
    parse_terminal_markers,
    validate_request_inputs,
)
from ..env import env_float
from .sync_sessions import (
    DEFAULT_FINAL_PULL_DEADLINE_SECONDS,
    InProcessControlPlaneView,
    LeaseService,
    SyncSessionService,
)


class SandboxService:
    """Facade over sandbox persistence, provisioning, the worker, and daemons."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        sandbox_backend: SandboxBackend,
        worker: SandboxWorker,
        mgmt_keys: MgmtKeyStore,
        metrics_archive: MetricsArchive,
        lease_client_id: str,
        activity: ActivityLogger | None = None,
        request_wait_seconds: float | None = None,
        stale_provision_seconds: float | None = None,
        experiments: ExperimentTransitions | None = None,
        blobs: BlobStore | None = None,
        quotas: QuotaAdmission | None = None,
        task_channel: TaskChannel | None = None,
    ) -> None:
        self.store = store
        self.backend = sandbox_backend
        self.activity = activity
        lease_client_id = str(lease_client_id or "").strip()
        if not lease_client_id:
            raise ValidationError("lease_client_id is required")
        if task_channel is None:
            raise ValidationError("task_channel is required")
        if not callable(getattr(task_channel, "submit", None)):
            raise ValidationError("task_channel.submit is required")
        if experiments is None:
            raise ValidationError("experiments is required")
        if not callable(getattr(experiments, "apply_system_transition", None)):
            raise ValidationError("experiments.apply_system_transition is required")
        if quotas is None:
            raise ValidationError("quotas is required")
        if not callable(getattr(quotas, "check_admission", None)):
            raise ValidationError("quotas.check_admission is required")
        # Cost governance (cloud plan Phase 7): admission gate at the
        # procurement choke point. The 'local' tenant has no quota row ⇒
        # unlimited ⇒ a no-op, so local mode is byte-identical.
        self.quotas = quotas
        # Per-sandbox management keypairs (plan Phase 5, fixed decision 4):
        # control-plane custody; transcript reads, metrics sampling, and the
        # parachute authenticate with these, never with the user key.
        self.mgmt_keys = mgmt_keys
        # The blob store holds parachute objects (decision 7's one shared
        # store). None means "no parachute home" — the branch then fails
        # LOUDLY (sandbox.parachute_failed), never silently.
        self.blobs = blobs
        # Sandbox lifecycle changes experiment status only through the workflow
        # engine's system transitions — never by writing the experiments table.
        self.experiments = experiments
        # All conn/tunnel/rsync work routes through the data-plane worker; the
        # facade owns no local-IO machinery of its own.
        self.worker = worker
        self.request_wait_seconds = env_float(
            "RESEARCH_PLUGIN_SANDBOX_REQUEST_WAIT",
            request_wait_seconds,
            DEFAULT_REQUEST_WAIT_SECONDS,
        )
        self.registry = SandboxRegistry(store=store)
        self.metrics = SandboxMetrics(
            registry=self.registry,
            backend=sandbox_backend,
            worker=self.worker,
            mgmt_keys=self.mgmt_keys,
            metrics_archive=metrics_archive,
            store=store,
        )
        self.parachute = SandboxParachute(
            registry=self.registry,
            backend=sandbox_backend,
            blobs=self.blobs,
            mgmt_keys=self.mgmt_keys,
            tasks=task_channel,
            worker=self.worker,
            tenant_for_project=lambda project_id: self.registry.tenant_for_project(
                project_id=project_id
            ),
        )
        # Backward-compatible public handles used by tests and thin UI helpers.
        self.metrics_archive = self.metrics.metrics_archive
        self.metrics_records = self.metrics.metrics_records
        # Data-plane work that deserves a record (tunnel came up) reports
        # through the registry's event stream.
        self.worker.set_event_sink(self.registry.emit_event)
        # Sync sessions + leases (plan Phase 4): every byte movement is
        # authorized by the experiment's exclusive lease — the cross-client
        # authority — and described by a session the worker executes. The lease
        # holder identity is injected by composition so control can use a
        # daemon/deployment identity without reaching into a local worker.
        self.leases = LeaseService(store=store)
        self.sessions = SyncSessionService(
            leases=self.leases,
            client_id=lease_client_id,
        )
        # The task channel (plan Phase 4): control enqueues, data executes.
        # Local composition injects a worker-backed in-process channel; split
        # control injects an HttpTaskChannel. This service is channel-blind and
        # never constructs data-plane machinery itself.
        self.tasks = task_channel
        # Marking a row failed/terminated also tears down its runtime
        # attachments; the registry stays persistence-only via this hook.
        self.registry.on_terminal = self._on_terminal_row
        self.provisioner = SandboxProvisioner(
            registry=self.registry,
            backend=sandbox_backend,
            experiments=self.experiments,
            worker=self.worker,
            sessions=self.sessions,
            tasks=self.tasks,
            refresh_row=self._refresh_row,
            stale_provision_seconds=env_float(
                "RESEARCH_PLUGIN_SANDBOX_STALE",
                stale_provision_seconds,
                DEFAULT_STALE_PROVISION_SECONDS,
            ),
        )
        # The poller's window onto "my running sandboxes + leases" — the call
        # that becomes the daemon's HTTP poll in Phase 8.
        self.control_view = InProcessControlPlaneView(
            registry=self.registry, sessions=self.sessions
        )
        self.daemons = SandboxDaemons(
            registry=self.registry,
            backend=sandbox_backend,
            provisioner=self.provisioner,
            experiments=self.experiments,
            control_view=self.control_view,
            sync_row=self._sync_row,
            final_pull=self._final_pull_row,
            persist_metrics=self.metrics.persist_row,
            parachute=self.parachute.rescue_row,
        )
        self.daemons.start()
        # Control-side transcript cursor cache (plan Phase 9, risk 14): coalesces
        # the UI's 3 s-per-viewer SSH transcript reads. Bounded + TTL'd; serves
        # the last full transcript per sandbox so `since=` polls stay cheap.
        self.transcript_cache = TranscriptCache()

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
        public_key_override: str | None = None,
        include_data_plane_enrichment: bool = True,
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

        if public_key_override:
            public_key = public_key_override
        else:
            public_key, _key_path = self._ensure_keypair(experiment_id=experiment_id)
        # Mint the management keypair before any provision so key injection
        # always precedes the management read paths (plan Phase 5 sequencing).
        management_public_key = self.mgmt_keys.ensure(experiment_id=experiment_id)

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
            if include_data_plane_enrichment:
                row = self.worker.ensure_local_dashboards(row=row)
            self.registry.emit_event(
                project_id=project_id,
                event_type="sandbox.reused",
                experiment_id=experiment_id,
                payload={"sandbox_id": existing["sandbox_id"]},
            )
            return self._agent_result(row=row, reused=True, include_data_plane_enrichment=include_data_plane_enrichment)

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

        # 2b) Cost-governance admission (cloud plan Phase 7). The choke point for
        #     a NEW provision: count the tenant's running sandboxes and check the
        #     request's time_limit + (resolvable) instance price against the
        #     tenant's ceilings. The 'local' tenant has no quota row ⇒ unlimited
        #     ⇒ this raises nothing, so local mode is unchanged. Reuse (step 1)
        #     is already past this gate — only fresh procurement is governed.
        self.quotas.check_admission(
            request=AdmissionRequest(
                tenant_id=self.registry.tenant_for_project(project_id=project_id),
                time_limit_seconds=int(time_limit),
                price_usd_per_hour=self._price_for_instance(
                    instance_type=instance_type, region=region
                ),
            )
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
            management_public_key=management_public_key,
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            instance_type=instance_type,
            region=region,
            # The remote folder is named after the experiment so the VM layout
            # mirrors experiments/<name>/ locally (id fallback for legacy rows).
            remote_workdir=remote_experiment_dir(
                experiment_id=experiment_id,
                name=self.registry.experiment_name(experiment_id=experiment_id),
            ),
        )
        job = self.provisioner.ensure_job(
            experiment_id=experiment_id,
            project_id=project_id,
            req=req,
            existing=existing,
        )
        job.done.wait(timeout=self.request_wait_seconds)
        row = self.registry.load_row(experiment_id=experiment_id)
        if include_data_plane_enrichment:
            row = self.worker.ensure_local_dashboards(row=row)
        reused = False if row.get("status") == "running" else None
        # Post-boot secret delivery (plan Phase 9, risk 16): once the VM is up,
        # push provider credentials over the management channel rather than
        # baking them into user_data. Best-effort and only on a fresh provision
        # (reuse already has them); never blocks or fails the request.
        if reused is False:
            self._deliver_secrets(row=row, experiment_id=experiment_id)
        return self._agent_result(row=row, reused=reused, include_data_plane_enrichment=include_data_plane_enrichment)

    def request_from_data_plane(
        self,
        *,
        experiment_id: str,
        public_key: str,
        project_id: str | None = None,
        gpu: str | None = None,
        cpu: float | None = None,
        memory: int | None = None,
        time_limit: int | None = None,
        instance_type: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        return self.request(
            experiment_id=experiment_id,
            project_id=project_id,
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            instance_type=instance_type,
            region=region,
            public_key_override=public_key,
            include_data_plane_enrichment=False,
        )

    def get(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        tenant_id: str | None = None,
        include_data_plane_enrichment: bool = True,
    ) -> dict[str, Any]:
        """Read-only poll target. Never provisions; reconciles stale state."""
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id, project_id=project_id, tenant_id=tenant_id
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
        row = self.provisioner.reconcile(row=row)
        # An unclaimed parachute lands the moment the data plane shows up
        # again (plan Phase 5): the poll target is that reconnect signal.
        if include_data_plane_enrichment:
            row = self._maybe_restore_parachute(row=row)
            row = self.worker.ensure_local_dashboards(row=row)
        return self._agent_result(
            row=row,
            reused=None,
            include_data_plane_enrichment=include_data_plane_enrichment,
        )

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

    def release(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        skip_final_pull: bool = False,
    ) -> dict[str, Any]:
        row = self.registry.fetch_scoped(experiment_id=experiment_id, project_id=project_id)
        # Signal any in-flight provisioning job to abort. It terminates whatever
        # it created via the cancel path (acquire's cleanup), even if create is
        # still mid-flight when we return.
        self.provisioner.cancel(experiment_id=experiment_id)
        stopped = False
        # Daemon-reachability signal for the UI (plan Phase 9): a failed final
        # pull means the data-plane daemon was unreachable (or its rsync broke),
        # so the parachute was used and the latest local files may be stale. The
        # release still proceeds — freeing billing always beats data recovery —
        # but the result flags it so the UI can show a "daemon unreachable" state.
        daemon_unreachable = False
        was_active = bool(row.get("sandbox_id") and row.get("status") in ACTIVE_SANDBOX_STATUSES)
        final_pull_skipped = bool(was_active and skip_final_pull)
        final_result: dict[str, Any] = {}
        if was_active and not skip_final_pull:
            try:
                final_result = self._final_pull_row(row=row)
            except Exception:  # noqa: BLE001 — release should still terminate
                # The agent is present at release, so the parachute only
                # fires when the pull itself failed — same injectable branch
                # as the reaper (plan Phase 5). Loud either way, never raises.
                daemon_unreachable = True
                self._parachute_row(row=row)
                final_result = {}
        if was_active:
            # Last chance to read MLflow: the server dies with the VM.
            self.metrics.persist_row(
                row=row,
                force=True,
                snapshot=final_result.get("metrics_snapshot"),
                snapshot_provided=True,
            )
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
            payload={
                "sandbox_id": row.get("sandbox_id", ""),
                "stopped": stopped,
                "daemon_unreachable": daemon_unreachable,
                "final_pull_skipped": final_pull_skipped,
            },
        )
        view = self._row_view(row=self.registry.load_row(experiment_id=experiment_id))
        view["daemon_unreachable"] = daemon_unreachable
        view["final_pull_skipped"] = final_pull_skipped
        if daemon_unreachable:
            view["hint"] = (
                "Sandbox terminated. The final pull failed, so local files may "
                "be stale. Inspect the local experiment folder before "
                "registering or associating resources, and rerun the experiment "
                "if required outputs are missing."
            )
        elif final_pull_skipped:
            view["hint"] = (
                "Sandbox terminated. Hosted control skipped the final pull "
                "because local files are owned by the data-plane daemon. Use "
                "sandbox.sync from the local daemon before release when a "
                "deliberate file handoff is required."
            )
        elif was_active:
            view["hint"] = (
                "Sandbox terminated. A best-effort final pull and metrics "
                "snapshot were attempted before termination. For deliberate "
                "handoff, prefer sandbox.sync before release and "
                "register/associate local resources before submitting results."
            )
        else:
            view["hint"] = (
                "Sandbox terminated. No running sandbox needed a final pull."
            )
        return view

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
        sandbox_id = str(row.get("sandbox_id") or "")
        full = ""
        unavailable = False

        def _read() -> str:
            return self.backend.read_transcript(
                sandbox_id=sandbox_id,
                experiment_id=experiment_id,
                volume_name=str(row.get("volume_name") or ""),
                workdir=str(row.get("workdir") or ""),
                tail=None,
                # Stored endpoint + the per-sandbox MANAGEMENT key (plan
                # Phase 5, fixed decision 4), for backends that read the
                # transcript over SSH (Lambda Labs). Control-plane property:
                # this read never touches the user key or the user's machine.
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                ssh_user=str(row.get("ssh_user") or ""),
                key_path=str(self.mgmt_keys.key_path(experiment_id=experiment_id)),
            )

        try:
            # Cursor cache (plan Phase 9, risk 14): repeated control-side reads
            # for the same sandbox within the TTL serve the cached full
            # transcript instead of re-hitting SSH every 3 s poll. `since=` is
            # applied to the cached bytes below, so incremental polls stay
            # correct AND cheap. A terminal sandbox can't produce more output,
            # so caching its transcript is always safe.
            full = self.transcript_cache.get_or_read(
                sandbox_id=sandbox_id, read=_read, since=since
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

    def sync(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        include_data_plane_metrics: bool = True,
        daemon_metrics_snapshot: dict[str, Any] | None = None,
        daemon_metrics_snapshot_provided: bool = False,
    ) -> dict[str, Any]:
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
        except (BackendUnavailableError, ResearchPluginError):
            # Domain errors stay actionable as-is — notably a sync lease held
            # by another client (plan Phase 4), which names the holder.
            raise
        except TimeoutError as exc:
            # The data-plane daemon never executed the sync task within budget
            # (split mode: daemon offline / long-poll asleep). Surface a clear
            # daemon-unreachable status the UI can render rather than a generic
            # backend error (plan Phase 9).
            raise BackendUnavailableError(
                "the data-plane daemon is unreachable, so the sandbox could not "
                "be synced; check that the local daemon is running and retry",
                details={"reason": "daemon_unreachable", "experiment_id": experiment_id},
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(f"sandbox sync failed: {exc}") from exc
        if include_data_plane_metrics:
            snapshot = (
                daemon_metrics_snapshot
                if daemon_metrics_snapshot_provided
                else result.get("metrics_snapshot")
            )
            self.metrics.persist_row(
                row=row,
                force=True,
                snapshot=snapshot if isinstance(snapshot, dict) else None,
                snapshot_provided=True,
            )
        self.registry.emit_event(
            project_id=str(row["project_id"]),
            event_type="sandbox.synced",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": row.get("sandbox_id", ""),
                "pulled": result.get("pulled", 0),
                "conflicts": result.get("conflicts", 0),
                # Logical (repo-relative) spelling: event payloads are
                # cloud-bound rows and must not carry absolute local paths.
                "local_dir": self.worker.repo_relative(result.get("local_dir", "")),
            },
        )
        has_conflicts = bool(result.get("conflicts") or result.get("skipped_conflicts"))
        hint = (
            "The experiment folder was pulled with rsync, but the "
            "local sync has conflicts. Resolve the reported conflict paths, then "
            "run sandbox.sync again before registering or associating resources."
            if has_conflicts
            else (
                "The sandbox's experiment folder has been mirrored back to the "
                "local repo (local files now match the sandbox exactly). Now "
                "register/associate local result files with "
                "resource.register_file and resource.associate before "
                "sandbox.release."
            )
        )
        return {
            "experiment_id": experiment_id,
            "project_id": row.get("project_id"),
            "sandbox_id": row.get("sandbox_id"),
            "status": row.get("status"),
            "workdir": row.get("workdir"),
            "experiment_dir": row.get("sync_dir") or row.get("workdir"),
            "sync_dir": row.get("sync_dir") or row.get("workdir"),
            "data_dir": row.get("sandbox_data_dir") or row.get("unsynced_dir") or "",
            "local_experiment_dir": self._local_sync_dir(experiment_id=experiment_id),
            "local_sync_dir": self._local_sync_dir(experiment_id=experiment_id),
            "sync": result,
            "hint": hint,
        }

    def record_daemon_metrics(
        self,
        *,
        experiment_id: str,
        project_id: str,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return self.metrics.record_daemon_metrics(
            experiment_id=experiment_id,
            project_id=project_id,
            snapshot=snapshot,
        )

    def results_metrics(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        return self.metrics.results_metrics(
            experiment_id=experiment_id, project_id=project_id
        )

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
        return self.worker.ensure_local_dashboards(row=self.provisioner.reconcile(row=row))

    def rows(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        """All sandbox rows for a project (most-recent first)."""
        return [
            self.worker.ensure_local_dashboards(row=row)
            for row in self.registry.list_rows(project_id=project_id)
        ]

    def row_view(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Public projection of a sandbox row (machine-local fields enriched
        by the worker). The HTTP layer shapes its responses from this."""
        return self._row_view(row=row)

    def backend_health(self) -> dict[str, Any]:
        """Full backend health payload (the slim ``health`` tool trims this)."""
        return self.backend.health()

    def sample_metrics(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any]:
        return self.metrics.sample_metrics(
            experiment_id=experiment_id, project_id=project_id
        )

    # ---------- workflow / home helpers ----------

    def sandboxes_for_experiment(self, *, conn, experiment_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM sandboxes WHERE experiment_id = ? ORDER BY created_seq DESC",
            (experiment_id,),
        ).fetchall()
        return [self._row_view(row=row_to_dict(row=row) or {}, conn=conn) for row in rows]

    def sandboxes_for_project(self, *, conn, project_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM sandboxes WHERE project_id = ? ORDER BY created_seq DESC",
            (project_id,),
        ).fetchall()
        return [self._row_view(row=row_to_dict(row=row) or {}, conn=conn) for row in rows]

    # ---------- lifecycle plumbing ----------

    def shutdown(self) -> None:
        """Stop the daemons, in-flight provisioning jobs, and tunnels."""
        self.daemons.stop()
        self.provisioner.shutdown()
        self.worker.stop_dashboards()

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

        A ``teardown`` task on the channel (plan Phase 4) — conn files and
        tunnels are data-plane property, so in split mode the daemon executes
        it from its task loop. ``sandbox_id`` is None when the row itself was
        missing — the task skips tunnel teardown but still drops the conn
        file, matching the pre-split behavior. The management keypair dies
        with the sandbox (per-sandbox keys, plan Phase 5): control-side
        custody, so it is dropped here rather than in the data-plane task.
        """
        try:
            self.mgmt_keys.remove(experiment_id=experiment_id)
        except Exception:  # noqa: BLE001 — key cleanup must never block the mark
            pass
        # Teardown is best-effort data-plane cleanup (conn files, tunnels). It
        # must never block or abort the terminal mark — in split mode the
        # HttpTaskChannel could time out (daemon long-poll asleep), and the
        # reaper still has to revert the experiment and free billing. The
        # daemon also drops stale conn state on its next reconnect/get.
        try:
            self.tasks.submit(
                task_type="teardown",
                payload={"experiment_id": experiment_id, "sandbox_id": sandbox_id},
                tenant_id=self._tenant_for_experiment(experiment_id=experiment_id),
            )
        except Exception:  # noqa: BLE001 — best-effort; never block the mark
            pass

    def _refresh_row(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Endpoint + provider-dashboard refresh for a confirmed-live row."""
        return self._maybe_refresh_dashboards(row=self._maybe_refresh_endpoint(row=row))

    def _maybe_refresh_dashboards(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Re-read provider-native dashboard URLs and persist if changed.

        Companion to the endpoint refresh: when a sandbox's tunnels move on
        the Modal side, the SSH host/port AND the dashboard HTTPS URLs all
        change together. These are provider-portable row facts — unlike the
        worker's loopback tunnels — so they live on the row. Best-effort: a
        backend without ``dashboard_urls`` or an error reading them leaves the
        stored value untouched.
        """
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id or row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            return row
        try:
            fresh = self.backend.dashboard_urls(sandbox_id=sandbox_id)
        except Exception:  # noqa: BLE001 — refresh must never break the caller
            return row
        if fresh is None or not isinstance(fresh, dict):
            return row
        normalized = {str(k): str(v) for k, v in fresh.items() if isinstance(v, str) and v}
        encoded = encode_dashboards(normalized)
        if encoded == (row.get("dashboards_json") or "{}"):
            return row
        experiment_id = str(row.get("experiment_id"))
        self.registry.upsert(experiment_id=experiment_id, dashboards_json=encoded)
        return self.registry.load_row(experiment_id=experiment_id)

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
        fresh = self.registry.load_row(experiment_id=experiment_id)
        # The agent's conn file must follow the endpoint: a conn_refresh task
        # re-renders it through the data plane (plan Phase 4). Best-effort,
        # like the refresh itself — the next agent view re-renders it anyway.
        try:
            self.tasks.submit(
                task_type="conn_refresh",
                payload={
                    "row": fresh,
                    "name": self.registry.experiment_name(experiment_id=experiment_id),
                },
                tenant_id=self.registry.tenant_for_project(project_id=str(fresh.get("project_id") or "")),
            )
        except Exception:  # noqa: BLE001 — refresh must never break the caller
            pass
        self.registry.emit_event(
            project_id=str(row.get("project_id")),
            event_type="sandbox.endpoint_refreshed",
            experiment_id=experiment_id,
            payload={"ssh_host": host, "ssh_port": port},
        )
        return fresh

    # ---------- sync engine (rsync work delegated to the worker) ----------

    def _sync_row(
        self,
        *,
        row: dict[str, Any],
        session: dict[str, Any] | None = None,
        skip_if_busy: bool = False,
    ) -> dict[str, Any]:
        experiment_id = str(row.get("experiment_id") or "")
        name = self.registry.experiment_name(experiment_id=experiment_id)
        # The lease authorizes the bytes (plan Phase 4): acquire — or renew,
        # for this client's own lease — before anything moves. The auto-sync
        # poller hands in the session its ControlPlaneView already granted.
        if session is None:
            session = self.sessions.grant_for_row(row=row, name=name)
        result = self.tasks.submit(
            task_type="sync_pull",
            payload={
                "session": session,
                "name": name,
                "skip_if_busy": bool(skip_if_busy),
                "row": row,
            },
            tenant_id=str(row.get("tenant_id") or self.registry.tenant_for_project(project_id=str(row.get("project_id") or ""))),
        )
        if not result.get("skipped"):
            self._report_pull(row=row, session=session, result=result)
        return result

    def _final_pull_row(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Last pull before terminate (release and the reaper).

        A ``final_pull`` task on the channel, with a cloud-minted deadline —
        unenforced in-process, where the local worker is always reachable.
        When it fails (or a split-mode daemon misses the deadline), the
        caller fires ``_parachute_row`` instead (plan Phase 5, decision 5).
        """
        experiment_id = str(row.get("experiment_id") or "")
        name = self.registry.experiment_name(experiment_id=experiment_id)
        session = self.sessions.grant_for_row(row=row, name=name)
        result = self.tasks.submit(
            task_type="final_pull",
            payload={"session": session, "name": name, "row": row},
            deadline=iso_after(seconds=DEFAULT_FINAL_PULL_DEADLINE_SECONDS),
            tenant_id=str(row.get("tenant_id") or self.registry.tenant_for_project(project_id=str(row.get("project_id") or ""))),
        )
        if not result.get("skipped"):
            self._report_pull(row=row, session=session, result=result)
        return result

    # ---------- expiry parachute (plan Phase 5, fixed decision 5) ----------

    def _parachute_row(self, *, row: dict[str, Any]) -> None:
        self.parachute.rescue_row(row=row)

    def _deliver_secrets(self, *, row: dict[str, Any], experiment_id: str) -> None:
        """Push provider credentials over the management channel post-boot.

        Replaces the cleartext-in-user_data token embed (plan Phase 9, risk 16).
        Only fires for a running row over a backend that has a secret channel
        and something to deliver. Best-effort: any failure (no mgmt key, VM not
        yet reachable, no tokens configured) is swallowed — the worst case is
        the agent's HF downloads lack a token, never a failed provision. The
        secret value is never logged.
        """
        if row.get("status") != "running":
            return
        sandbox_id = str(row.get("sandbox_id") or "")
        if not sandbox_id:
            return
        try:
            secrets = self.backend.sandbox_secrets()
        except Exception:  # noqa: BLE001
            secrets = {}
        if not secrets:
            return
        try:
            self.backend.write_secrets(
                sandbox_id=sandbox_id,
                secrets=secrets,
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                key_path=str(self.mgmt_keys.key_path(experiment_id=experiment_id)),
            )
        except Exception:  # noqa: BLE001 — secret delivery must never fail a request
            pass

    def _maybe_restore_parachute(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return self.parachute.maybe_restore_row(row=row)

    def _report_pull(
        self, *, row: dict[str, Any], session: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Record a completed pull, lease-checked (``sandbox_report_sync``, §3.1).

        A stale or foreign lease id — another client took the experiment over
        mid-sync — is rejected with an actionable error before any record is
        written, so a superseded holder never credits its own pull.
        """
        self.sessions.report_completion(
            experiment_id=str(row.get("experiment_id") or ""),
            lease_id=str((session.get("lease") or {}).get("id") or ""),
        )
        self._record_pull(row=row, result=result)

    def _record_pull(self, *, row: dict[str, Any], result: dict[str, Any]) -> None:
        self.registry.emit_event(
            project_id=str(row.get("project_id")),
            event_type="sandbox.rsynced",
            experiment_id=str(row.get("experiment_id") or ""),
            payload={
                "sandbox_id": row.get("sandbox_id", ""),
                "pulled": result.get("pulled", 0),
                # Logical (repo-relative) spelling: event payloads are
                # cloud-bound rows and must not carry absolute local paths.
                "local_dir": self.worker.repo_relative(result.get("local_dir", "")),
                "duration_seconds": result.get("duration_seconds", 0),
            },
        )

    # ---------- paths / conn-file plumbing (delegated to the worker) ----------

    def _local_sync_dir(self, *, experiment_id: str) -> Path:
        return self.worker.local_experiment_dir(
            experiment_id=experiment_id,
            name=self.registry.experiment_name(experiment_id=experiment_id),
        )

    def _ensure_keypair(self, *, experiment_id: str) -> tuple[str, Path]:
        return self.worker.ensure_keypair(experiment_id=experiment_id)

    # ---------- views (delegated to sandbox_views) ----------

    def _agent_result(
        self,
        *,
        row: dict[str, Any],
        reused: bool | None,
        include_data_plane_enrichment: bool,
    ) -> dict[str, Any]:
        if include_data_plane_enrichment:
            return self._agent_view(row=row, reused=reused)
        return self._agent_facts(row=row, reused=reused)

    def _agent_facts(self, *, row: dict[str, Any], reused: bool | None) -> dict[str, Any]:
        experiment_id = str(row.get("experiment_id") or "")
        return sandbox_views.agent_row_facts(
            row=row,
            env_info=self._sandbox_environment(),
            reused=reused,
            lease=self.leases.holder(experiment_id=experiment_id),
        )

    def _agent_view(self, *, row: dict[str, Any], reused: bool | None) -> dict[str, Any]:
        # Plane decomposition (plan §3.3): provider-portable row facts are a
        # pure projection; the ssh command / key path / local folder come from
        # the worker. Local mode merges them here, so tool results are
        # unchanged; split mode performs the same merge across the seam.
        experiment_id = str(row.get("experiment_id") or "")
        facts = sandbox_views.agent_row_facts(
            row=row,
            env_info=self._sandbox_environment(),
            reused=reused,
            lease=self.leases.holder(experiment_id=experiment_id),
        )
        enrichment = self.worker.sandbox_enrichment(
            row=row,
            name=self.registry.experiment_name(experiment_id=experiment_id),
        )
        return sandbox_views.merge_agent_view(facts=facts, enrichment=enrichment)

    def _agent_summary(self, *, row: dict[str, Any]) -> dict[str, Any]:
        experiment_id = str(row.get("experiment_id") or "")
        return sandbox_views.agent_summary(
            row=row, lease=self.leases.holder(experiment_id=experiment_id)
        )

    def _row_view(
        self, *, row: dict[str, Any], conn: Connection | None = None
    ) -> dict[str, Any]:
        experiment_id = str(row.get("experiment_id") or "")
        row = self.worker.merge_local_dashboards(row=row)
        return sandbox_views.sandbox_row_view(
            row=row,
            local_sync_dir=str(
                self.worker.local_experiment_dir(
                    experiment_id=experiment_id,
                    name=self._experiment_name(experiment_id=experiment_id, conn=conn),
                )
            ),
        )

    def _experiment_name(
        self, *, experiment_id: str, conn: Connection | None = None
    ) -> str:
        # Conn-scoped callers (workflow status) resolve the folder name on
        # their own connection instead of opening a fresh one per row.
        if conn is None:
            return self.registry.experiment_name(experiment_id=experiment_id)
        row = conn.execute(
            "SELECT name FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        return str(row["name"]) if row is not None and row["name"] else ""

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

    def _tenant_for_experiment(self, *, experiment_id: str) -> str:
        conn = self.store.connect()
        try:
            row = conn.execute(
                """
                SELECT p.tenant_id
                FROM experiments e
                JOIN projects p ON p.id = e.project_id
                WHERE e.id = ?
                """,
                (experiment_id,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT tenant_id FROM sandboxes WHERE experiment_id = ?",
                    (experiment_id,),
                ).fetchone()
        finally:
            conn.close()
        return str(row["tenant_id"]) if row is not None else "local"

    def _price_for_instance(
        self, *, instance_type: str | None, region: str | None
    ) -> float | None:
        """Resolve the catalog price for a chosen SKU, for the quota price gate.

        Best-effort: only meaningful for bundled-hardware backends that expose a
        catalog with prices (Lambda, the fake's selection mode). Returns None
        when there is no instance_type or no matching priced option, in which
        case quota admission skips the price ceiling (Modal has no per-hour quote).
        """
        if not instance_type:
            return None
        try:
            catalog = self.backend.hardware_catalog(region=region)
        except Exception:  # noqa: BLE001 — admission must not hinge on a catalog call
            return None
        if not catalog:
            return None
        for option in catalog.get("options", []) or []:
            if str(option.get("instance_type") or "") == instance_type:
                price = option.get("price_usd_per_hour")
                return float(price) if price is not None else None
        return None

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
