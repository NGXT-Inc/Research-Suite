"""Sandbox lifecycle facade: provision SSH windows, observe them, release them."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ...domain.sandbox_paths import remote_experiment_dir

from ...state.activity import ActivityLogger
from ...state.store import BaseStateStore, Connection, row_to_dict
from ...utils import (
    NotFoundError,
    ValidationError,
    format_iso,
    parse_iso,
)
from ...sandbox.sandbox_backend import (
    BackendCapabilities,
    BackendValidationError,
    SandboxBackend,
    SandboxRequest,
    TranscriptTail,
)
from ...ports.mgmt_keys import MgmtKeyStore
from ...ports.quota_admission import AdmissionRequest, QuotaAdmission
from ...ports.sandbox_worker import SandboxWorker
from ...ports.task_channel import TaskChannel
from . import sandbox_views
from .sandbox_heartbeat import SandboxActivityPolicy
from .sandbox_lifecycle import SandboxLifecycle
from .sandbox_metrics import SandboxMetrics
from .sandbox_runs import SandboxRunLedger, run_records_view
from ..transcript_cache import TranscriptCache
from .sandbox_daemons import SandboxDaemons
from .sandbox_provisioner import SandboxProvisioner
from .sandbox_registry import SandboxRegistry
from ...sandbox.sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    DEFAULT_REQUEST_WAIT_SECONDS,
    DEFAULT_STALE_PROVISION_SECONDS,
    MAX_TIME_LIMIT_SECONDS,
    RUNS_WAIT_CAP_SECONDS,
    RUNS_WAIT_POLL_SECONDS,
    parse_terminal_markers,
    parse_terminal_snapshot,
    validate_request_inputs,
)
from ...env import env_float


class SandboxService:
    """Facade over sandbox persistence, provisioning, the worker, and daemons."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        sandbox_backend: SandboxBackend,
        worker: SandboxWorker,
        mgmt_keys: MgmtKeyStore,
        activity: ActivityLogger | None = None,
        request_wait_seconds: float | None = None,
        stale_provision_seconds: float | None = None,
        quotas: QuotaAdmission | None = None,
        task_channel: TaskChannel | None = None,
        storage_enabled: bool = False,
        storage_hint: str = "",
        attachment_check: Callable[..., None] | None = None,
        force_expiry_reaper: bool = False,
    ) -> None:
        self.store = store
        self.backend = sandbox_backend
        self.activity = activity
        if task_channel is None:
            raise ValidationError("task_channel is required")
        if not callable(getattr(task_channel, "submit", None)):
            raise ValidationError("task_channel.submit is required")
        if quotas is None:
            raise ValidationError("quotas is required")
        if not callable(getattr(quotas, "check_admission", None)):
            raise ValidationError("quotas.check_admission is required")
        if not callable(getattr(quotas, "check_lifetime_extension", None)):
            raise ValidationError("quotas.check_lifetime_extension is required")
        # Cost governance (cloud plan Phase 7): admission gate at the
        # procurement choke point. The 'local' tenant has no quota row ⇒
        # unlimited ⇒ a no-op, so local mode is byte-identical.
        self.quotas = quotas
        # Per-sandbox management keypairs (plan Phase 5, fixed decision 4):
        # control-plane custody; transcript reads and metrics sampling use
        # these, never the user key.
        self.mgmt_keys = mgmt_keys
        # Conn files and local tunnels route through the data-plane worker.
        self.worker = worker
        self.storage_enabled = bool(storage_enabled)
        # Guidance prose for durable heavy-file storage. Injected by the
        # composition root (it owns the storage feature); the sandbox module
        # embeds the string it is handed and never imports storage guidance.
        self.storage_hint = str(storage_hint or "")
        # Optional surface-owned validator for attachment labels. The sandbox
        # module treats attachment ids as opaque strings; only the surface
        # knows a label happens to be an experiment id, so the composition
        # injects the existence/scope check. None ⇒ ids arrive pre-validated.
        self.attachment_check = attachment_check
        self.activity_policy = SandboxActivityPolicy()
        self.request_wait_seconds = env_float(
            "RESEARCH_PLUGIN_SANDBOX_REQUEST_WAIT",
            request_wait_seconds,
            DEFAULT_REQUEST_WAIT_SECONDS,
        )
        self.registry = SandboxRegistry(store=store)
        self.metrics = SandboxMetrics(
            registry=self.registry,
            backend=sandbox_backend,
            mgmt_keys=self.mgmt_keys,
        )
        # merv_run receipts mirror: reconciled by the daemon sweep and on
        # sandbox.runs reads; source of the live-runs nudge line.
        self.runs_ledger = SandboxRunLedger(
            store=store,
            registry=self.registry,
            backend=sandbox_backend,
            mgmt_keys=self.mgmt_keys,
        )
        self.runs_wait_poll_seconds = RUNS_WAIT_POLL_SECONDS
        # The task channel is only for cross-plane conn refresh and teardown.
        self.tasks = task_channel
        # Sandboxes whose post-boot secrets were already pushed (in-process;
        # a daemon restart just re-delivers once, which the push tolerates).
        self._secrets_delivered: set[str] = set()
        # Single owner of status transitions and destructive decisions:
        # terminal marks (incl. teardown), VM termination, reconcile, reaping.
        # Registry stays persistence-only; provisioner keeps job threads;
        # daemons keep scheduling.
        self.lifecycle = SandboxLifecycle(
            registry=self.registry,
            backend=sandbox_backend,
            mgmt_keys=self.mgmt_keys,
            tasks=task_channel,
        )
        self.provisioner = SandboxProvisioner(
            registry=self.registry,
            backend=sandbox_backend,
            lifecycle=self.lifecycle,
            stale_provision_seconds=env_float(
                "RESEARCH_PLUGIN_SANDBOX_STALE",
                stale_provision_seconds,
                DEFAULT_STALE_PROVISION_SECONDS,
            ),
        )
        # The one inversion left: reconcile asks whether a provisioning row is
        # still owned by a live job thread in this process.
        self.lifecycle.job_probe = self.provisioner.job_is_live
        self.daemons = SandboxDaemons(
            registry=self.registry,
            backend=sandbox_backend,
            provisioner=self.provisioner,
            lifecycle=self.lifecycle,
            sample_metrics=self.metrics.sample_metrics,
            reconcile_runs=self.runs_ledger.reconcile_live,
            force_expiry_reaper=force_expiry_reaper,
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
        experiment_id: str | None = None,
        project_id: str | None = None,
        gpu: str | None = None,
        cpu: float | None = None,
        memory: int | None = None,
        time_limit: int | None = None,
        instance_type: str | None = None,
        region: str | None = None,
        provider: str | None = None,
        public_key: str | None = None,
        public_key_override: str | None = None,
        include_data_plane_enrichment: bool = True,
        additional: bool = False,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        experiment_id = (experiment_id or "").strip()
        provider = (provider or "").strip() or None
        # Per-request capabilities: the provider value routes to the serving
        # backend (multiplexed deployments); a single backend ignores it.
        caps = self._capabilities_for(provider=provider)
        gpu, cpu, memory, time_limit = validate_request_inputs(
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            configurable_resources=caps.configurable_resources,
        )
        instance_type = (instance_type or "").strip() or None
        region = (region or "").strip() or None

        # Resolve scope. Experiment attachment is optional and is represented
        # only by sandbox_attachments; it never controls sandbox eligibility.
        # The label's existence/scope check is surface-injected: the sandbox
        # module treats the attachment id as opaque.
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
        if experiment_id and self.attachment_check is not None:
            self.attachment_check(
                attachment_id=experiment_id, project_id=project_id
            )
        if experiment_id:
            try:
                existing = self.registry.load_row(experiment_id=experiment_id)
            except NotFoundError:
                existing = None
        else:
            existing = None
            additional = False

        requested_uid = (sandbox_uid or "").strip()
        sandbox_uid = (
            requested_uid
            or (
                self.registry.new_sandbox_uid()
                if additional
                else str(
                    (existing or {}).get("sandbox_uid")
                    or self.registry.new_sandbox_uid()
                )
            )
        )
        supplied_public_key = (
            str(public_key_override).strip()
            if public_key_override is not None
            else str(public_key or "").strip()
        )
        if not supplied_public_key:
            raise ValidationError(
                "sandbox.request requires public_key; generate a caller-owned "
                "OpenSSH keypair and pass the single-line .pub contents"
            )
        public_key = supplied_public_key
        public_key_source = "caller"
        # Mint the management keypair before any provision so key injection
        # always precedes the management read paths (plan Phase 5 sequencing).
        management_public_key = self.mgmt_keys.ensure(sandbox_uid=sandbox_uid)

        # 1) Reuse a live sandbox immediately — the common mid-session case.
        if (
            not additional
            and existing
            and existing.get("status") in ACTIVE_SANDBOX_STATUSES
            and existing.get("sandbox_id")
            # Unknown liveness (provider blip) still reuses: a dead SSH target
            # fails loudly and cheaply, whereas falling through to ensure_job
            # would cleanup_orphan — i.e. TERMINATE — a possibly-healthy VM.
            and self.lifecycle.liveness(sandbox_id=str(existing["sandbox_id"]))
            is not False
        ):
            self.registry.touch_alive(
                experiment_id=experiment_id,
                sandbox_uid=str(existing.get("sandbox_uid") or ""),
            )
            row = self.lifecycle.refresh_endpoint(
                row=self.registry.get_by_uid(
                    sandbox_uid=str(existing.get("sandbox_uid") or "")
                )
            )
            self.registry.emit_event(
                project_id=project_id,
                event_type="sandbox.reused",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": existing["sandbox_id"],
                    "sandbox_uid": existing.get("sandbox_uid", ""),
                    "active_experiment_ids": self.registry.active_experiment_ids(
                        sandbox_uid=str(existing.get("sandbox_uid") or "")
                    ),
                },
            )
            result = self._agent_result(
                row=row,
                reused=True,
                include_data_plane_enrichment=include_data_plane_enrichment,
                use_sandbox_uid_command=True,
            )
            result["public_key_source"] = public_key_source
            return result

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
                    instance_type=instance_type, region=region, provider=caps.name
                ),
            )
        )

        # 3) Otherwise (re)start provisioning in the background and best-effort
        #    wait up to the budget. A big first sync or a cold GPU returns
        #    `provisioning` (the agent polls sandbox.get); a fast one returns SSH
        #    inline, exactly like before. Backend errors are handled inside the
        #    job, which lands the row in `failed` — so request never times out.
        remote_dir = remote_experiment_dir(
            experiment_id=sandbox_uid,
            name=f"sandbox-{sandbox_uid[:12]}",
        )
        req = SandboxRequest(
            experiment_id=sandbox_uid,
            project_id=project_id,
            public_key=public_key,
            sandbox_uid=sandbox_uid,
            management_public_key=management_public_key,
            management_key_path=str(self.mgmt_keys.key_path(sandbox_uid=sandbox_uid)),
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            time_limit=time_limit,
            instance_type=instance_type,
            region=region,
            provider=provider,
            # A sandbox is a machine first: its workdir is sandbox-owned. Any
            # experiment relationship lives only in sandbox_attachments.
            remote_workdir=remote_dir,
            public_key_source=public_key_source,
        )
        job = self.provisioner.ensure_job(
            experiment_id=experiment_id,
            project_id=project_id,
            req=req,
            existing=None if additional else existing,
            sandbox_uid=sandbox_uid,
            create_new=additional,
        )
        job.done.wait(timeout=self.request_wait_seconds)
        row = self.registry.get_by_uid(sandbox_uid=sandbox_uid)
        reused = False if row.get("status") == "running" else None
        # Post-boot secret delivery (plan Phase 9, risk 16): once the VM is up,
        # push provider credentials over the management channel rather than
        # baking them into user_data. Best-effort; never blocks or fails the
        # request. Slow provisions (any real GPU boot outlives the wait above)
        # deliver from the agent's next sandbox.get poll instead.
        self._deliver_secrets_once(row=row, experiment_id=experiment_id)
        result = self._agent_result(
            row=row,
            reused=reused,
            include_data_plane_enrichment=include_data_plane_enrichment,
            use_sandbox_uid_command=True,
        )
        result["public_key_source"] = public_key_source
        return result

    def request_from_data_plane(
        self,
        *,
        experiment_id: str | None = None,
        public_key: str,
        project_id: str | None = None,
        gpu: str | None = None,
        cpu: float | None = None,
        memory: int | None = None,
        time_limit: int | None = None,
        instance_type: str | None = None,
        region: str | None = None,
        provider: str | None = None,
        additional: bool = False,
        sandbox_uid: str | None = None,
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
            provider=provider,
            public_key_override=public_key,
            include_data_plane_enrichment=False,
            additional=additional,
            sandbox_uid=sandbox_uid,
        )

    def get(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
        include_data_plane_enrichment: bool = True,
    ) -> dict[str, Any]:
        """Read-only poll target. Never provisions; reconciles stale state."""
        experiment_id = (experiment_id or "").strip()
        if not experiment_id and not (sandbox_uid or "").strip():
            raise ValidationError("sandbox.get requires experiment_id or sandbox_uid")
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id,
                project_id=project_id,
                tenant_id=tenant_id,
                sandbox_uid=sandbox_uid,
            )
        except NotFoundError:
            # Soften only the genuine "never provisioned" case so the poll loop
            # never has to catch an exception. A project-scope mismatch (the row
            # exists under another project) is a real error and still raises.
            if (sandbox_uid or "").strip():
                raise
            if experiment_id and self.registry.exists(experiment_id=experiment_id):
                raise
            return {
                "experiment_id": experiment_id,
                "status": "none",
                "hint": "No sandbox for this experiment — call sandbox.request to create one.",
            }
        row = self.lifecycle.reconcile(row=row)
        # First poll that observes "running" delivers the post-boot secrets a
        # slow provision missed in request()'s bounded wait.
        self._deliver_secrets_once(row=row, experiment_id=experiment_id)
        return self._agent_result(
            row=row,
            reused=None,
            include_data_plane_enrichment=include_data_plane_enrichment,
            use_sandbox_uid_command=True,
        )

    def attach(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        sandbox_uid: str,
        include_data_plane_enrichment: bool = True,
        public_key_override: str | None = None,
    ) -> dict[str, Any]:
        """Associate a running sandbox with another experiment."""
        _ = public_key_override
        sandbox_uid = sandbox_uid.strip()
        if not sandbox_uid:
            raise ValidationError("sandbox.attach requires sandbox_uid")
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
        finally:
            conn.close()
        try:
            source_row = self.registry.get_by_uid(sandbox_uid=sandbox_uid)
        except NotFoundError as exc:
            raise NotFoundError(f"sandbox not found: {sandbox_uid}") from exc
        if source_row.get("project_id") != project_id:
            raise NotFoundError(f"sandbox not found in project {project_id}: {sandbox_uid}")
        source_row = self.lifecycle.reconcile(row=source_row)
        if source_row.get("status") != "running" or not source_row.get("sandbox_id"):
            raise ValidationError("sandbox.attach requires a running sandbox")
        if self.lifecycle.liveness(sandbox_id=str(source_row["sandbox_id"])) is False:
            raise ValidationError("sandbox.attach requires a live sandbox")
        if self.attachment_check is not None:
            self.attachment_check(
                attachment_id=experiment_id, project_id=project_id
            )
        row = self.registry.attach(
            sandbox_uid=sandbox_uid,
            experiment_id=experiment_id,
            project_id=project_id,
        )
        active_experiment_ids = self.registry.active_experiment_ids(
            sandbox_uid=sandbox_uid
        )
        self.registry.emit_event(
            project_id=project_id,
            event_type="sandbox.attached",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": row.get("sandbox_id", ""),
                "sandbox_uid": sandbox_uid,
                "active_experiment_ids": active_experiment_ids,
            },
        )
        result = self._agent_result(
            row=row,
            reused=True,
            include_data_plane_enrichment=include_data_plane_enrichment,
            use_sandbox_uid_command=True,
        )
        result["active_experiment_ids"] = active_experiment_ids
        return result

    def attach_from_data_plane(
        self,
        *,
        experiment_id: str,
        sandbox_uid: str,
        public_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return self.attach(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
            public_key_override=public_key,
            include_data_plane_enrichment=False,
        )

    def extend(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
        seconds: int = 1800,
    ) -> dict[str, Any]:
        """Extend a live sandbox's reaper deadline by one bounded increment."""
        experiment_id = (experiment_id or "").strip()
        sandbox_uid = (sandbox_uid or "").strip()
        if not experiment_id and not sandbox_uid:
            raise ValidationError("sandbox.extend requires experiment_id or sandbox_uid")
        seconds = int(seconds)
        if seconds <= 0 or seconds > 1800:
            raise ValidationError("sandbox.extend seconds must be between 1 and 1800")
        row = self.registry.fetch_scoped(
            experiment_id=experiment_id,
            project_id=project_id,
            tenant_id=tenant_id,
            sandbox_uid=sandbox_uid,
        )
        # Capabilities of the backend that owns THIS row (its provider column;
        # empty = the configured default backend).
        caps = self._capabilities_for(provider=str(row.get("provider") or "") or None)
        if not caps.lifetime_extension_supported:
            raise ValidationError(
                f"{caps.name} sandboxes do not support lifetime extension"
            )
        row = self.lifecycle.reconcile(row=row)
        if row.get("status") not in ACTIVE_SANDBOX_STATUSES:
            raise ValidationError("sandbox.extend requires a running sandbox")
        expires_at = parse_iso(row.get("expires_at"))
        if expires_at is None:
            raise ValidationError("sandbox.extend requires an existing expires_at deadline")
        current_limit = int(row.get("time_limit") or 0)
        new_limit = current_limit + seconds
        if new_limit > MAX_TIME_LIMIT_SECONDS:
            raise ValidationError(
                f"sandbox.extend would exceed the max lifetime ({MAX_TIME_LIMIT_SECONDS}s)"
            )
        resolved_project_id = str(row.get("project_id") or project_id or "")
        tenant = str(row.get("tenant_id") or self.registry.tenant_for_project(
            project_id=resolved_project_id
        ))
        price = row.get("price_usd_per_hour")
        self.quotas.check_lifetime_extension(
            tenant_id=tenant,
            total_time_limit_seconds=new_limit,
            price_usd_per_hour=float(price) if price is not None else None,
        )
        if not self.activity_policy.is_active_snapshot(
            snapshot=self.registry.heartbeat_snapshot(row=row),
            command=self.registry.command_snapshot(row=row),
        ):
            raise ValidationError(
                "sandbox.extend requires a running command or active heartbeat metrics"
            )
        old_expires_at = str(row.get("expires_at") or "")
        new_expires_at = format_iso(expires_at + timedelta(seconds=seconds))
        updated = self.registry.extend_lifetime(
            sandbox_uid=str(row.get("sandbox_uid") or ""),
            expires_at=new_expires_at,
            time_limit=new_limit,
        )
        resolved_experiment_id = experiment_id or str(updated.get("experiment_id") or "")
        self.registry.emit_event(
            project_id=resolved_project_id,
            event_type="sandbox.lifetime_extended",
            experiment_id=resolved_experiment_id,
            payload={
                "sandbox_id": updated.get("sandbox_id", ""),
                "sandbox_uid": updated.get("sandbox_uid", ""),
                "old_expires_at": old_expires_at,
                "expires_at": new_expires_at,
                "seconds": seconds,
                "time_limit": new_limit,
            },
        )
        view = self._agent_result(
            row=updated,
            reused=None,
            include_data_plane_enrichment=False,
            use_sandbox_uid_command=True,
        )
        view["extended"] = True
        view["old_expires_at"] = old_expires_at
        view["extended_by_seconds"] = seconds
        view["time_limit"] = new_limit
        return view

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
            "sandbox.request(instance_type=..., region=?). Include experiment_id "
            "only when attaching the sandbox to an experiment. "
            "Options are sorted cheapest-first and reflect live capacity."
            if selection_required
            else (
                "Call sandbox.request(gpu=?, cpu=?, memory=?). Include "
                "experiment_id only when attaching the sandbox to an experiment. "
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
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
        confirm_retained: bool = False,
    ) -> dict[str, Any]:
        experiment_id = (experiment_id or "").strip()
        if not experiment_id and not (sandbox_uid or "").strip():
            raise ValidationError("sandbox.release requires experiment_id or sandbox_uid")
        row = self.registry.fetch_scoped(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
        )
        targets = [row]
        if experiment_id and not sandbox_uid:
            rows = [
                item
                for item in self.registry.list_by_experiment(experiment_id=experiment_id)
                if item.get("project_id") == row.get("project_id")
            ]
            active = [
                item
                for item in rows
                if item.get("status") in (ACTIVE_SANDBOX_STATUSES | {"provisioning"})
            ]
            if len(active) > 1:
                targets = active
        # Retention gate: deleting a sandbox is irreversible, so the first call
        # confirms nothing was lost before we destroy the VM. Re-call with
        # confirm_retained=true to actually terminate.
        if not confirm_retained:
            # The live-runs line matters most right here: releasing a box with
            # a live run kills the run.
            return self._with_runs_nudge(
                view=self._release_confirmation(
                    experiment_id=experiment_id,
                    project_id=str(row.get("project_id") or ""),
                    targets=targets,
                ),
                sandbox_uid=str(row.get("sandbox_uid") or ""),
            )
        views = [self._release_row(row=target) for target in targets]
        if len(views) == 1:
            return views[0]
        return {
            "experiment_id": experiment_id,
            "project_id": row.get("project_id"),
            "status": "terminated",
            "released_count": len(views),
            "sandboxes": views,
            "hint": "All live sandboxes for this experiment were terminated.",
        }

    def _release_confirmation(
        self, *, experiment_id: str, project_id: str, targets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """First-call retention checklist: release does not delete until the
        agent confirms it kept what it needs — the VM is destroyed on release."""
        pending = [
            {
                "sandbox_uid": str(target.get("sandbox_uid") or ""),
                "sandbox_id": str(target.get("sandbox_id") or ""),
                "status": target.get("status"),
                "workdir": target.get("workdir"),
            }
            for target in targets
        ]
        count = len(pending)
        noun = "sandbox" if count == 1 else "sandboxes"
        return {
            "experiment_id": experiment_id,
            "project_id": project_id,
            "status": "confirmation_required",
            "released": False,
            "pending_release": pending,
            "hint": (
                f"Not released yet. This will permanently destroy {count} {noun} "
                "and everything on the VM. First confirm you have retained "
                "everything you need: rsync the light files you want off the box "
                "yourself over SSH into the local work folder"
                + (
                    f", and storage.upload_file for durable "
                    f"heavy artifacts. {self.storage_hint}"
                    if self.storage_enabled
                    else "; heavy-file storage is not enabled on this backend"
                )
                + ". Nothing is copied automatically — anything you do "
                "not pull is lost. When you have everything, re-call sandbox.release "
                "with confirm_retained=true to terminate."
            ),
        }

    def _release_row(self, *, row: dict[str, Any]) -> dict[str, Any]:
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        # Signal any in-flight provisioning job to abort. It terminates whatever
        # it created via the cancel path (acquire's cleanup), even if create is
        # still mid-flight when we return.
        self.provisioner.cancel(experiment_id=experiment_id, sandbox_uid=sandbox_uid)
        was_active = bool(row.get("sandbox_id") and row.get("status") in ACTIVE_SANDBOX_STATUSES)
        # Direct terminate applies only to rows the provider may still be
        # running; anything else goes straight to orphan cleanup + confirm.
        outcome = self.lifecycle.terminate_vm(
            row=row,
            try_direct=bool(
                row.get("sandbox_id")
                and row.get("status") in (ACTIVE_SANDBOX_STATUSES | {"provisioning"})
            ),
        )
        if outcome == "maybe_alive":
            # Never strand the row as terminated over a possibly-live VM (it
            # would bill invisibly forever). Left running, a retried release
            # or the expiry reaper finishes the job.
            self.registry.emit_event(
                project_id=str(row["project_id"]),
                event_type="sandbox.release_failed",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": row.get("sandbox_id", ""),
                    "sandbox_uid": sandbox_uid,
                    "reason": "terminate failed; instance may still be alive",
                },
            )
            view = self._row_view(
                row=self.registry.get_by_uid(sandbox_uid=sandbox_uid)
            )
            view["hint"] = (
                "Release did NOT complete: the provider terminate call "
                "failed and the VM may still be running (and billing). "
                "The sandbox stays active; retry sandbox.release, or the "
                "expiry reaper will retry at the deadline."
            )
            return view
        stopped = outcome == "stopped"
        self.lifecycle.mark_terminated(
            experiment_id=experiment_id, sandbox_uid=sandbox_uid
        )
        self.registry.emit_event(
            project_id=str(row["project_id"]),
            event_type="sandbox.released",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": row.get("sandbox_id", ""),
                "sandbox_uid": sandbox_uid,
                "active_experiment_ids": self._active_experiment_ids_for_row(row=row),
                "stopped": stopped,
            },
        )
        view = self._row_view(row=self.registry.get_by_uid(sandbox_uid=sandbox_uid))
        if was_active:
            view["hint"] = (
                "Sandbox terminated. The VM and files on it are gone. Only "
                "files the agent explicitly copied or uploaded before release "
                "remain durable."
            )
        else:
            view["hint"] = (
                "Sandbox terminated. No running sandbox needed teardown."
            )
        return view

    def terminal(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
        tail: int | None = None,
        since: int | None = None,
    ) -> dict[str, Any]:
        """Read a sandbox terminal transcript.

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

        Cursor semantics: backends return a bounded tail window (~50KB) plus
        the transcript's TRUE byte size, and ``cursor``/``since`` are raw byte
        offsets into the full transcript. The cursor therefore keeps advancing
        after the log outgrows the window, and a ``since`` that has slid out of
        the window clamps to the window start (older bytes are gone from the
        tail and cannot be replayed).
        """
        experiment_id = (experiment_id or "").strip()
        if not experiment_id and not (sandbox_uid or "").strip():
            raise ValidationError("sandbox.terminal requires experiment_id or sandbox_uid")
        row = self.registry.fetch_scoped(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
        )
        status = str(row.get("status", "none"))
        sandbox_id = str(row.get("sandbox_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        resolved_experiment_id = experiment_id or str(row.get("experiment_id") or "")
        transcript_key = sandbox_uid or resolved_experiment_id
        unavailable = False

        def _read_for(key: str) -> TranscriptTail:
            return self.backend.read_transcript(
                sandbox_id=sandbox_id,
                experiment_id=key,
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
                key_path=str(self._mgmt_key_path(row=row)),
            )

        def _read() -> TranscriptTail:
            window = _read_for(transcript_key)
            if (
                window.data
                or window.total_bytes
                or not resolved_experiment_id
                or resolved_experiment_id == transcript_key
            ):
                return window
            return _read_for(resolved_experiment_id)

        window = TranscriptTail(data=b"", total_bytes=0)
        try:
            # Cursor cache (plan Phase 9, risk 14): repeated control-side reads
            # for the same sandbox within the TTL serve the cached tail window
            # instead of re-hitting SSH every 3 s poll. `since=` is applied to
            # the cached bytes below, so incremental polls stay correct AND
            # cheap. A terminal sandbox can't produce more output, so caching
            # its transcript is always safe.
            window = self.transcript_cache.get_or_read(
                sandbox_id=sandbox_id, read=_read, since=since
            )
        except Exception as exc:  # noqa: BLE001
            full = f"(terminal unavailable: {exc})"
            unavailable = True
        if unavailable:
            transcript = full
            cursor = len(full)
        else:
            # The window is the last len(window.data) bytes of a transcript
            # that is really total_bytes long; cursor math happens in absolute
            # byte offsets so it stays valid once the log outgrows the window.
            # Slice the raw bytes BEFORE decoding: errors="replace" can change
            # byte counts, which would skew the cursor.
            cursor = window.total_bytes
            window_start = max(cursor - len(window.data), 0)
            if since is not None:
                start = min(max(int(since) - window_start, 0), len(window.data))
                raw = window.data[start:]
            elif tail is not None and tail >= 0 and len(window.data) > tail:
                raw = window.data[-tail:]
            else:
                raw = window.data
            transcript = raw.decode("utf-8", errors="replace")
            full = window.data.decode("utf-8", errors="replace")
        # Parse exit markers from the FULL transcript (the last one may predate
        # the `since` cursor) and gate "command running" on the sandbox actually
        # being alive — a dead sandbox isn't running a command, even if its log
        # ends on a command-start marker with no recorded exit. On a successful
        # read, persist the snapshot so the next SSH failure can still return a
        # useful last-known command status.
        last_command: dict[str, Any] | None = None
        command_status_stale = False
        if unavailable:
            last_command = self.registry.command_snapshot(row=row)
            command_status_stale = last_command is not None
            last_exit_code = (
                None if last_command is None else last_command.get("exit_code")
            )
            last_command_finished_at = (
                None if last_command is None else last_command.get("finished_at")
            )
            command_running = (
                None
                if last_command is None
                else (
                    last_command.get("status") == "running"
                    and status in ACTIVE_SANDBOX_STATUSES
                )
            )
        else:
            snapshot = parse_terminal_snapshot(full)
            if (
                snapshot.get("status") == "running"
                and status not in ACTIVE_SANDBOX_STATUSES
            ):
                snapshot = {**snapshot, "status": "interrupted"}
            last_command = (
                self.registry.record_command_snapshot(
                    sandbox_uid=sandbox_uid,
                    snapshot=snapshot,
                )
                if snapshot.get("command_id")
                else None
            )
            last_exit_code, last_command_finished_at, in_flight = parse_terminal_markers(full)
            command_running = in_flight and status in ACTIVE_SANDBOX_STATUSES
        return self._with_runs_nudge(
            view={
                "experiment_id": resolved_experiment_id,
                "sandbox_uid": sandbox_uid,
                "sandbox_id": row.get("sandbox_id", ""),
                "status": status,
                "running": status in ACTIVE_SANDBOX_STATUSES,
                "transcript": transcript,
                "cursor": cursor,
                "new_chars": len(transcript) if since is not None else None,
                "last_exit_code": last_exit_code,
                "last_command_finished_at": last_command_finished_at,
                "command_running": command_running,
                "last_command": last_command,
                "command_status_stale": command_status_stale,
            },
            sandbox_uid=sandbox_uid,
        )

    def runs(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        """merv_run receipts for a sandbox or an experiment's sandboxes.

        Reconciles live boxes on the spot, so the answer is fresh even between
        daemon sweeps, and still answers from the mirror after the box died or
        was released. ``wait_seconds`` long-polls: the server re-lists every
        few seconds and returns early when any run reaches a terminal state
        (or there is nothing left running) — one slow call instead of N
        transcript polls.
        """
        experiment_id = (experiment_id or "").strip()
        sandbox_uid = (sandbox_uid or "").strip()
        if not experiment_id and not sandbox_uid:
            raise ValidationError("sandbox.runs requires experiment_id or sandbox_uid")
        # Scope check up front (project/tenant mismatch must raise); a missing
        # LIVE sandbox is fine — receipts outlive the box, so serve the mirror.
        try:
            self.registry.fetch_scoped(
                experiment_id=experiment_id,
                project_id=project_id,
                tenant_id=tenant_id,
                sandbox_uid=sandbox_uid or None,
            )
        except NotFoundError:
            if sandbox_uid:
                raise
        wait = min(max(float(wait_seconds or 0), 0.0), RUNS_WAIT_CAP_SECONDS)
        deadline = time.monotonic() + wait
        baseline_finished: set[tuple[str, str]] | None = None
        while True:
            self._reconcile_runs_targets(
                experiment_id=experiment_id, sandbox_uid=sandbox_uid
            )
            records = (
                self.runs_ledger.records_for_sandbox(sandbox_uid=sandbox_uid)
                if sandbox_uid
                else self.runs_ledger.records_for_experiment(
                    experiment_id=experiment_id
                )
            )
            finished_now = {
                (str(r.get("sandbox_uid") or ""), str(r.get("label") or ""))
                for r in records
                if r.get("exit_code") is not None
            }
            if baseline_finished is None:
                baseline_finished = finished_now
            still_running = any(r.get("exit_code") is None for r in records)
            if (
                finished_now - baseline_finished
                or not still_running
                or time.monotonic() >= deadline
            ):
                return run_records_view(
                    records=records,
                    experiment_id=experiment_id,
                    sandbox_uid=sandbox_uid,
                )
            time.sleep(
                min(self.runs_wait_poll_seconds, max(deadline - time.monotonic(), 0.1))
            )

    def _reconcile_runs_targets(
        self, *, experiment_id: str, sandbox_uid: str
    ) -> None:
        """Fresh receipts for every live sandbox in scope; dead ones keep the mirror."""
        if sandbox_uid:
            try:
                rows = [self.registry.get_by_uid(sandbox_uid=sandbox_uid)]
            except NotFoundError:
                rows = []
        else:
            rows = self.registry.list_by_experiment(experiment_id=experiment_id)
        for row in rows:
            self.runs_ledger.reconcile_row(row=row)

    def health(self) -> dict[str, Any]:
        health = self.backend.health()
        result = {
            "ok": bool(health.get("ok")),
        }
        if not result["ok"] and health.get("error"):
            result["error"] = health["error"]
        return result

    # ---------- domain primitives for the HTTP/UI layer ----------
    #
    # The service returns raw rows and sampled data; the HTTP layer shapes the
    # UI responses (see ResearchHttpApi.sandbox_*_view). This keeps presentation
    # out of the domain service.

    def get_row(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any] | None:
        """Reconciled sandbox row for an experiment or sandbox UID."""
        try:
            row = self.registry.fetch_scoped(
                experiment_id=(experiment_id or ""),
                project_id=project_id,
                sandbox_uid=sandbox_uid,
            )
        except NotFoundError:
            return None
        return self.lifecycle.reconcile(row=row)

    def rows(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        """All sandbox rows for a project (most-recent first)."""
        return self.registry.list_rows(project_id=project_id)

    def row_view(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Public projection of a sandbox row (machine-local fields enriched
        by the worker). The HTTP layer shapes its responses from this."""
        return self._row_view(row=row)

    def backend_health(self) -> dict[str, Any]:
        """Full backend health payload (the slim ``health`` tool trims this)."""
        return dict(self.backend.health())

    def sample_metrics(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        return self.metrics.sample_metrics(
            experiment_id=experiment_id,
            project_id=project_id,
            sandbox_uid=sandbox_uid,
        )

    # ---------- workflow / home helpers ----------

    def sandboxes_for_experiment(self, *, conn, experiment_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT s.*
            FROM sandboxes s
            JOIN sandbox_attachments a ON a.sandbox_uid = s.sandbox_uid
            WHERE a.experiment_id = ? AND a.detached_at IS NULL
            ORDER BY s.created_seq DESC
            """,
            (experiment_id,),
        ).fetchall()
        return [
            self._row_view(
                row={**(row_to_dict(row=row) or {}), "experiment_id": experiment_id},
                conn=conn,
            )
            for row in rows
        ]

    def sandboxes_for_project(self, *, conn, project_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM sandboxes WHERE project_id = ? ORDER BY created_seq DESC",
            (project_id,),
        ).fetchall()
        return [self._row_view(row=row_to_dict(row=row) or {}, conn=conn) for row in rows]

    # ---------- lifecycle plumbing ----------

    def shutdown(self) -> None:
        """Stop the daemons and in-flight provisioning jobs."""
        self.daemons.stop()
        self.provisioner.shutdown()

    def reap_expired(self, **kwargs: Any) -> int:
        """Terminate running sandboxes past expires_at (see SandboxLifecycle)."""
        return self.lifecycle.reap_expired(**kwargs)

    def reap_idle(self, **kwargs: Any) -> int:
        """Terminate running sandboxes idle past the heartbeat threshold."""
        return self.daemons.reap_idle(**kwargs)

    def reconcile_running_rows(self) -> int:
        """Reconcile every running row against the provider (startup/sweeps).

        The public surface for the cloud recovery paths (CleanupService,
        control-mode restart) — they must not reach through the facade into
        the registry/lifecycle. Returns how many rows left ``running``.
        Best-effort per row.
        """
        left_running = 0
        for row in self.registry.list_running_rows():
            try:
                fresh = self.lifecycle.reconcile(row=row)
            except Exception:  # noqa: BLE001 — one bad row never aborts the pass
                continue
            if (fresh or {}).get("status") != "running":
                left_running += 1
        return left_running

    def reap_stale_provisions(self, *, now: datetime, deadline_seconds: float) -> int:
        """Reap provisioning rows wedged past the deadline (see provisioner)."""
        return self.provisioner.reap_stale_provisions(
            now=now, deadline_seconds=deadline_seconds
        )

    def _deliver_secrets_once(self, *, row: dict[str, Any], experiment_id: str) -> None:
        """Deliver post-boot secrets the first time a running row is observed."""
        uid = str(row.get("sandbox_uid") or "")
        if not uid or row.get("status") != "running" or uid in self._secrets_delivered:
            return
        self._deliver_secrets(row=row, experiment_id=experiment_id)
        self._secrets_delivered.add(uid)

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
                key_path=str(self._mgmt_key_path(row=row)),
            )
        except Exception:  # noqa: BLE001 — secret delivery must never fail a request
            pass

    # ---------- paths / management-key plumbing ----------

    def _mgmt_key_path(self, *, row: dict[str, Any]) -> Path:
        return self.mgmt_keys.key_path(sandbox_uid=str(row.get("sandbox_uid") or ""))

    # ---------- views (delegated to sandbox_views) ----------

    def _agent_result(
        self,
        *,
        row: dict[str, Any],
        reused: bool | None,
        include_data_plane_enrichment: bool,
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        view = (
            self._agent_view(
                row=row,
                reused=reused,
                use_sandbox_uid_command=use_sandbox_uid_command,
            )
            if include_data_plane_enrichment
            else self._agent_facts(row=row, reused=reused)
        )
        return self._with_runs_nudge(
            view=view, sandbox_uid=str(row.get("sandbox_uid") or "")
        )

    def _with_runs_nudge(
        self, *, view: dict[str, Any], sandbox_uid: str
    ) -> dict[str, Any]:
        """Live-runs discovery: one compact receipts line on sandbox-scoped
        responses whenever runs exist, absent otherwise. Mirror-only read —
        never adds a remote round-trip to an unrelated tool call."""
        if not sandbox_uid:
            return view
        try:
            nudge = self.runs_ledger.nudge_line(sandbox_uid=sandbox_uid)
        except Exception:  # noqa: BLE001 — the nudge must never break a tool
            return view
        if nudge:
            view["runs"] = nudge
        return view

    def _agent_facts(self, *, row: dict[str, Any], reused: bool | None) -> dict[str, Any]:
        row = self._with_active_experiment_ids(row=row)
        return sandbox_views.agent_row_facts(
            row=row,
            env_info=self._sandbox_environment(),
            reused=reused,
            storage_enabled=self.storage_enabled,
        )

    def _agent_view(
        self,
        *,
        row: dict[str, Any],
        reused: bool | None,
        use_sandbox_uid_command: bool = True,
    ) -> dict[str, Any]:
        # Provider-portable row facts are a pure projection. A composition may
        # add local conveniences, but the current agent topology relies only on
        # SSH facts; caller key paths and checkout folders remain proxy-local.
        row = self._with_active_experiment_ids(row=row)
        sandbox_uid = str(row.get("sandbox_uid") or "")
        view_name = f"sandbox-{sandbox_uid[:12]}"
        facts = sandbox_views.agent_row_facts(
            row=row,
            env_info=self._sandbox_environment(),
            reused=reused,
            storage_enabled=self.storage_enabled,
        )
        enrichment = self.worker.sandbox_enrichment(
            row=row,
            name=view_name,
            use_sandbox_uid_command=use_sandbox_uid_command,
        )
        return sandbox_views.merge_agent_view(
            facts=facts, enrichment=enrichment, storage_hint=self.storage_hint
        )

    def _agent_summary(self, *, row: dict[str, Any]) -> dict[str, Any]:
        return sandbox_views.agent_summary(row=self._with_active_experiment_ids(row=row))

    def _row_view(
        self, *, row: dict[str, Any], conn: Connection | None = None
    ) -> dict[str, Any]:
        row = self._with_active_experiment_ids(row=row)
        sandbox_uid = str(row.get("sandbox_uid") or "")
        local_key = sandbox_uid
        local_name = f"sandbox-{sandbox_uid[:12]}"
        return sandbox_views.sandbox_row_view(
            row=row,
            local_sync_dir=str(
                self.worker.local_experiment_dir(
                    experiment_id=local_key,
                    name=local_name,
                )
            ),
        )

    def _active_experiment_ids_for_row(self, *, row: dict[str, Any]) -> list[str]:
        raw = row.get("active_experiment_ids")
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item)]
        sandbox_uid = str(row.get("sandbox_uid") or "")
        if not sandbox_uid:
            return []
        return self.registry.active_experiment_ids(sandbox_uid=sandbox_uid)

    def _with_active_experiment_ids(self, *, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        active = self._active_experiment_ids_for_row(row=row)
        out["active_experiment_ids"] = active
        if not out.get("experiment_id") and active:
            out["experiment_id"] = active[0]
        return out

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

    def _capabilities_for(self, *, provider: str | None) -> BackendCapabilities:
        """Per-request capability resolution; unknown providers fail cleanly."""
        try:
            return self.backend.capabilities_for(provider=provider)
        except BackendValidationError as exc:
            raise ValidationError(str(exc)) from exc

    def _price_for_instance(
        self,
        *,
        instance_type: str | None,
        region: str | None,
        provider: str | None = None,
    ) -> float | None:
        """Resolve the catalog price for a chosen SKU, for the quota price gate.

        Best-effort: only meaningful for bundled-hardware backends that expose a
        catalog with prices (Lambda, the fake's selection mode). Returns None
        when there is no instance_type or no matching priced option, in which
        case quota admission skips the price ceiling (Modal has no per-hour quote).
        ``provider`` narrows a multi-provider catalog to the serving backend's
        options (untagged options — single-backend catalogs — always match).
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
            if str(option.get("instance_type") or "") != instance_type:
                continue
            tagged = str(option.get("provider") or "")
            if provider and tagged and tagged != provider:
                continue
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
