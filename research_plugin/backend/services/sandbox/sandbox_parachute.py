"""Expiry parachute rescue and restore policy for sandbox rows."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from ...ports.mgmt_keys import MgmtKeyStore
from ...ports.sandbox_worker import SandboxWorker
from ...ports.task_channel import TaskChannel
from ...sandbox_backend import SandboxBackend
from ...sandbox_support import (
    PARACHUTE_MAX_OBJECT_BYTES,
    PARACHUTE_TTL_SECONDS,
    iso_after,
)
from ...state.blobs import BlobStore
from ...utils import ValidationError
from .sandbox_registry import SandboxRegistry

TenantResolver = Callable[[str], str]


class SandboxParachute:
    """Rescue and restore experiment files when final pull is unavailable."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        blobs: BlobStore | None,
        mgmt_keys: MgmtKeyStore,
        tasks: TaskChannel,
        worker: SandboxWorker,
        tenant_for_project: TenantResolver,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.blobs = blobs
        self.mgmt_keys = mgmt_keys
        self.tasks = tasks
        self.worker = worker
        self.tenant_for_project = tenant_for_project

    def rescue_row(self, *, row: dict[str, Any]) -> None:
        """Rescue the experiment dir over the management channel."""
        experiment_id = str(row.get("experiment_id") or "")
        project_id = str(row.get("project_id") or "")
        sandbox_id = str(row.get("sandbox_id") or "")
        try:
            if self.blobs is None:
                raise ValidationError(
                    "no blob store is configured to hold parachute objects"
                )
            expires_at = iso_after(seconds=PARACHUTE_TTL_SECONDS)
            target = self.blobs.presign_put(
                namespace=project_id,
                max_size_bytes=PARACHUTE_MAX_OBJECT_BYTES,
                expires_at=expires_at,
                content_type="application/gzip",
            )
            receipt = self.backend.run_parachute(
                sandbox_id=sandbox_id,
                put_url=str(target.get("url") or ""),
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                key_path=str(self.mgmt_keys.key_path(experiment_id=experiment_id)),
            )
            if receipt is None:
                raise ValidationError("backend has no parachute channel")
            stat = self.blobs.finalize_put(upload_id=str(target["upload_id"]))
            if receipt.get("sha256") and str(receipt["sha256"]) != stat.sha256:
                raise ValidationError(
                    "parachute upload hash mismatch: VM reported "
                    f"{receipt['sha256']}, store landed {stat.sha256}"
                )
            self.registry.upsert(
                experiment_id=experiment_id,
                parachute_state="uploaded",
                parachute_object_key=f"{stat.namespace}/{stat.sha256}",
                parachute_sha256=stat.sha256,
                parachute_size_bytes=int(stat.size_bytes),
                parachute_expires_at=expires_at,
            )
            self.registry.emit_event(
                project_id=project_id,
                event_type="sandbox.parachuted",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": sandbox_id,
                    "object_key": f"{stat.namespace}/{stat.sha256}",
                    "sha256": stat.sha256,
                    "size_bytes": int(stat.size_bytes),
                    "expires_at": expires_at,
                },
            )
        except Exception as exc:  # noqa: BLE001 - loud event, terminate anyway
            with contextlib.suppress(Exception):
                self.registry.upsert(
                    experiment_id=experiment_id, parachute_state="failed"
                )
            with contextlib.suppress(Exception):
                self.registry.emit_event(
                    project_id=project_id,
                    event_type="sandbox.parachute_failed",
                    experiment_id=experiment_id,
                    payload={"sandbox_id": sandbox_id, "error": str(exc)},
                )

    def maybe_restore_row(self, *, row: dict[str, Any]) -> dict[str, Any]:
        """Land an unclaimed parachute in the local experiment folder."""
        if str(row.get("parachute_state") or "") != "uploaded":
            return row
        experiment_id = str(row.get("experiment_id") or "")
        project_id = str(row.get("project_id") or "")
        name = self.registry.experiment_name(experiment_id=experiment_id)
        try:
            if self.blobs is None:
                raise ValidationError(
                    "no blob store is configured to hold parachute objects"
                )
            sha256 = str(row.get("parachute_sha256") or "")
            download = self.blobs.presign_get(namespace=project_id, sha256=sha256)
            get_url = str(download.get("url") or "")
            if not get_url:
                raise ValidationError("blob store returned no parachute download URL")
            result = self.tasks.submit(
                task_type="parachute_restore",
                payload={
                    "experiment_id": experiment_id,
                    "name": name,
                    "get_url": get_url,
                },
                tenant_id=str(
                    row.get("tenant_id") or self.tenant_for_project(project_id)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - loud failure, no silent loop
            self.registry.upsert(experiment_id=experiment_id, parachute_state="failed")
            self.registry.emit_event(
                project_id=project_id,
                event_type="sandbox.parachute_failed",
                experiment_id=experiment_id,
                payload={
                    "sandbox_id": str(row.get("sandbox_id") or ""),
                    "stage": "restore",
                    "error": str(exc),
                },
            )
            return self.registry.load_row(experiment_id=experiment_id)
        self.registry.upsert(experiment_id=experiment_id, parachute_state="restored")
        self.registry.emit_event(
            project_id=project_id,
            event_type="sandbox.parachute_restored",
            experiment_id=experiment_id,
            payload={
                "sandbox_id": str(row.get("sandbox_id") or ""),
                "object_key": str(row.get("parachute_object_key") or ""),
                "restored": int(result.get("restored") or 0),
                "local_dir": self.worker.repo_relative(result.get("local_dir", "")),
            },
        )
        return self.registry.load_row(experiment_id=experiment_id)
