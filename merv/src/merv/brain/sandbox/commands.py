"""Typed Sandbox lifecycle command handler."""

from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ..kernel.ports.quota_admission import AdmissionRequest
from ..kernel.utils import NotFoundError, ValidationError, format_iso, parse_iso
from . import sandbox_views
from .lifecycle_reducer import release_decision
from .sandbox_backend import SandboxRequest
from .sandbox_paths import remote_experiment_dir
from .sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    MAX_TIME_LIMIT_SECONDS,
    validate_request_inputs,
)

if TYPE_CHECKING:
    from .facade import SandboxFacade


class SandboxCommandHandler:
    def __init__(self, host: SandboxFacade) -> None:
        self.store = host.store
        self.attachment_check = host.attachment_check
        self.repository = host.repository
        self.mgmt_keys = host.mgmt_keys
        self.lifecycle = host.lifecycle
        self.quotas = host.quotas
        self.provisioner = host.provisioner
        self.request_wait_seconds = host.request_wait_seconds
        self.activity_policy = host.activity_policy
        self.storage_enabled = host.storage_enabled
        self.storage_hint = host.storage_hint
        self._active_experiment_ids_for_row = host._active_experiment_ids_for_row
        self._agent_result = host._agent_result
        self._capabilities_for = host._capabilities_for
        self._deliver_secrets_once = host._deliver_secrets_once
        self._hardware_catalog = host._hardware_catalog
        self._price_for_instance = host._price_for_instance
        self._row_view = host._row_view
        self._with_runs_nudge = host._with_runs_nudge

    def execute_request(
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
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
        if experiment_id and self.attachment_check is not None:
            self.attachment_check(attachment_id=experiment_id, project_id=project_id)
        if experiment_id:
            try:
                existing = self.repository.load_row(experiment_id=experiment_id)
            except NotFoundError:
                existing = None
        else:
            existing = None
            additional = False
        requested_uid = (sandbox_uid or "").strip()
        sandbox_uid = requested_uid or (
            self.repository.new_sandbox_uid()
            if additional
            else str(
                (existing or {}).get("sandbox_uid") or self.repository.new_sandbox_uid()
            )
        )
        supplied_public_key = (
            str(public_key_override).strip()
            if public_key_override is not None
            else str(public_key or "").strip()
        )
        if not supplied_public_key:
            raise ValidationError(
                "sandbox.request requires public_key; generate a caller-owned OpenSSH keypair and pass the single-line .pub contents"
            )
        public_key = supplied_public_key
        public_key_source = "caller"
        management_public_key = self.mgmt_keys.ensure(sandbox_uid=sandbox_uid)
        if (
            not additional
            and existing
            and (existing.get("status") in ACTIVE_SANDBOX_STATUSES)
            and existing.get("sandbox_id")
            and (
                self.lifecycle.liveness(sandbox_id=str(existing["sandbox_id"]))
                is not False
            )
        ):
            self.repository.touch_alive(
                experiment_id=experiment_id,
                sandbox_uid=str(existing.get("sandbox_uid") or ""),
            )
            row = self.lifecycle.refresh_endpoint(
                row=self.repository.get_by_uid(
                    sandbox_uid=str(existing.get("sandbox_uid") or "")
                )
            )
            self.repository.emit_event(
                project_id=project_id,
                event_type="sandbox.reused",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": existing["sandbox_id"],
                    "sandbox_uid": existing.get("sandbox_uid", ""),
                    "active_experiment_ids": self.repository.active_experiment_ids(
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
        if caps.requires_hardware_selection and (not instance_type):
            catalog = self._hardware_catalog(gpu=gpu, region=region)
            return sandbox_views.needs_selection_view(
                experiment_id=experiment_id, project_id=project_id, catalog=catalog
            )
        self.quotas.check_admission(
            request=AdmissionRequest(
                tenant_id=self.repository.tenant_for_project(project_id=project_id),
                time_limit_seconds=int(time_limit),
                price_usd_per_hour=self._price_for_instance(
                    instance_type=instance_type, region=region, provider=caps.name
                ),
            )
        )
        remote_dir = remote_experiment_dir(
            experiment_id=sandbox_uid, name=f"sandbox-{sandbox_uid[:12]}"
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
        row = self.repository.get_by_uid(sandbox_uid=sandbox_uid)
        reused = False if row.get("status") == "running" else None
        self._deliver_secrets_once(row=row, experiment_id=experiment_id)
        result = self._agent_result(
            row=row,
            reused=reused,
            include_data_plane_enrichment=include_data_plane_enrichment,
            use_sandbox_uid_command=True,
        )
        result["public_key_source"] = public_key_source
        return result

    def execute_attach(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        sandbox_uid: str,
        include_data_plane_enrichment: bool = True,
        public_key_override: str | None = None,
    ) -> dict[str, Any]:
        _ = public_key_override
        sandbox_uid = sandbox_uid.strip()
        if not sandbox_uid:
            raise ValidationError("sandbox.attach requires sandbox_uid")
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
        try:
            source_row = self.repository.get_by_uid(sandbox_uid=sandbox_uid)
        except NotFoundError as exc:
            raise NotFoundError(f"sandbox not found: {sandbox_uid}") from exc
        if source_row.get("project_id") != project_id:
            raise NotFoundError(
                f"sandbox not found in project {project_id}: {sandbox_uid}"
            )
        source_row = self.lifecycle.reconcile(row=source_row)
        if source_row.get("status") != "running" or not source_row.get("sandbox_id"):
            raise ValidationError("sandbox.attach requires a running sandbox")
        if self.lifecycle.liveness(sandbox_id=str(source_row["sandbox_id"])) is False:
            raise ValidationError("sandbox.attach requires a live sandbox")
        if self.attachment_check is not None:
            self.attachment_check(attachment_id=experiment_id, project_id=project_id)
        row = self.repository.attach(
            sandbox_uid=sandbox_uid, experiment_id=experiment_id, project_id=project_id
        )
        active_experiment_ids = self.repository.active_experiment_ids(
            sandbox_uid=sandbox_uid
        )
        self.repository.emit_event(
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

    def execute_extend(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        sandbox_uid: str | None = None,
        seconds: int = 1800,
    ) -> dict[str, Any]:
        experiment_id = (experiment_id or "").strip()
        sandbox_uid = (sandbox_uid or "").strip()
        if not experiment_id and (not sandbox_uid):
            raise ValidationError(
                "sandbox.extend requires experiment_id or sandbox_uid"
            )
        seconds = int(seconds)
        if seconds <= 0 or seconds > 1800:
            raise ValidationError("sandbox.extend seconds must be between 1 and 1800")
        row = self.repository.fetch_scoped(
            experiment_id=experiment_id,
            project_id=project_id,
            tenant_id=tenant_id,
            sandbox_uid=sandbox_uid,
        )
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
            raise ValidationError(
                "sandbox.extend requires an existing expires_at deadline"
            )
        current_limit = int(row.get("time_limit") or 0)
        new_limit = current_limit + seconds
        if new_limit > MAX_TIME_LIMIT_SECONDS:
            raise ValidationError(
                f"sandbox.extend would exceed the max lifetime ({MAX_TIME_LIMIT_SECONDS}s)"
            )
        resolved_project_id = str(row.get("project_id") or project_id or "")
        tenant = str(
            row.get("tenant_id")
            or self.repository.tenant_for_project(project_id=resolved_project_id)
        )
        price = row.get("price_usd_per_hour")
        self.quotas.check_lifetime_extension(
            tenant_id=tenant,
            total_time_limit_seconds=new_limit,
            price_usd_per_hour=float(price) if price is not None else None,
        )
        if not self.activity_policy.is_active_snapshot(
            snapshot=self.repository.heartbeat_snapshot(row=row),
            command=self.repository.command_snapshot(row=row),
        ):
            raise ValidationError(
                "sandbox.extend requires a running command or active heartbeat metrics"
            )
        old_expires_at = str(row.get("expires_at") or "")
        new_expires_at = format_iso(expires_at + timedelta(seconds=seconds))
        updated = self.repository.extend_lifetime(
            sandbox_uid=str(row.get("sandbox_uid") or ""),
            expires_at=new_expires_at,
            time_limit=new_limit,
        )
        resolved_experiment_id = experiment_id or str(
            updated.get("experiment_id") or ""
        )
        self.repository.emit_event(
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

    def execute_release(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
        confirm_retained: bool = False,
    ) -> dict[str, Any]:
        experiment_id = (experiment_id or "").strip()
        if not experiment_id and (not (sandbox_uid or "").strip()):
            raise ValidationError(
                "sandbox.release requires experiment_id or sandbox_uid"
            )
        row = self.repository.fetch_scoped(
            experiment_id=experiment_id, project_id=project_id, sandbox_uid=sandbox_uid
        )
        targets = [row]
        if experiment_id and (not sandbox_uid):
            rows = [
                item
                for item in self.repository.list_by_experiment(
                    experiment_id=experiment_id
                )
                if item.get("project_id") == row.get("project_id")
            ]
            active = [
                item
                for item in rows
                if item.get("status") in ACTIVE_SANDBOX_STATUSES | {"provisioning"}
            ]
            if len(active) > 1:
                targets = active
        if not confirm_retained:
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
            "hint": f"Not released yet. This will permanently destroy {count} {noun} and everything on the VM. First confirm you have retained everything you need: rsync the light files you want off the box yourself over SSH into the local work folder"
            + (
                f", and storage.upload_file for durable heavy artifacts. {self.storage_hint}"
                if self.storage_enabled
                else "; heavy-file storage is not enabled on this backend"
            )
            + ". Nothing is copied automatically — anything you do not pull is lost. When you have everything, re-call sandbox.release with confirm_retained=true to terminate.",
        }

    def _release_row(self, *, row: dict[str, Any]) -> dict[str, Any]:
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_uid = str(row.get("sandbox_uid") or "")
        self.provisioner.cancel(experiment_id=experiment_id, sandbox_uid=sandbox_uid)
        was_active = bool(
            row.get("sandbox_id") and row.get("status") in ACTIVE_SANDBOX_STATUSES
        )
        outcome = self.lifecycle.terminate_vm(
            row=row,
            try_direct=bool(
                row.get("sandbox_id")
                and row.get("status") in ACTIVE_SANDBOX_STATUSES | {"provisioning"}
            ),
        )
        decision = release_decision(
            row=row,
            outcome=outcome,
            active_experiment_ids=self._active_experiment_ids_for_row(row=row),
        )
        self.lifecycle.apply(row=row, decision=decision)
        if outcome == "maybe_alive":
            view = self._row_view(row=self.repository.get_by_uid(sandbox_uid=sandbox_uid))
            view["hint"] = (
                "Release did NOT complete: the provider terminate call failed and the VM may still be running (and billing). The sandbox stays active; retry sandbox.release, or the expiry reaper will retry at the deadline."
            )
            return view
        view = self._row_view(row=self.repository.get_by_uid(sandbox_uid=sandbox_uid))
        if was_active:
            view["hint"] = (
                "Sandbox terminated. The VM and files on it are gone. Only files the agent explicitly copied or uploaded before release remain durable."
            )
        else:
            view["hint"] = "Sandbox terminated. No running sandbox needed teardown."
        return view


__all__ = ["SandboxCommandHandler"]
