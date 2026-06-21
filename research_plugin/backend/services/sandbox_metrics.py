"""Sandbox metrics coordination kept out of the sandbox facade."""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any

from ..ports.metrics_archive import MetricsArchive
from ..ports.mgmt_keys import MgmtKeyStore
from ..ports.sandbox_worker import SandboxWorker
from ..sandbox_backend import SandboxBackend
from ..sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    METRICS_CACHE_TTL_SECONDS,
    METRICS_PERSIST_TTL_SECONDS,
)
from ..state.store import BaseStateStore
from ..utils import NotFoundError, now_iso
from .metrics_records import MetricsSnapshotStore
from .sandbox_registry import SandboxRegistry


class SandboxMetrics:
    """Durable and live metrics behavior for sandbox rows."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        worker: SandboxWorker,
        mgmt_keys: MgmtKeyStore,
        metrics_archive: MetricsArchive,
        store: BaseStateStore,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.worker = worker
        self.mgmt_keys = mgmt_keys
        self.metrics_archive = metrics_archive
        self.metrics_records = MetricsSnapshotStore(store=store)
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._lock = threading.Lock()
        self._persisted_at: dict[str, float] = {}

    def record_daemon_metrics(
        self,
        *,
        experiment_id: str,
        project_id: str,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        row = self.registry.fetch_scoped(
            experiment_id=experiment_id,
            project_id=project_id,
        )
        self.persist_row(
            row=row,
            force=True,
            snapshot=snapshot if isinstance(snapshot, dict) else None,
            snapshot_provided=True,
        )
        return {"experiment_id": experiment_id, "recorded": isinstance(snapshot, dict)}

    def results_metrics(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Archived MLflow metrics for an experiment."""
        status = "none"
        row: dict[str, Any] | None = None
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id, project_id=project_id
            )
            status = str(row.get("status") or "none")
        except NotFoundError:
            if self.registry.exists(experiment_id=experiment_id):
                raise
        data = self.metrics_records.load(experiment_id=experiment_id)
        from_record = data is not None
        if data is None:
            data = self.metrics_archive.load(experiment_id=experiment_id)
        if data is None:
            snapshot = self.worker.capture_metrics_fallback(
                experiment_id=experiment_id,
                name=self.registry.experiment_name(experiment_id=experiment_id),
            )
            if snapshot is not None:
                with contextlib.suppress(OSError):
                    self.metrics_archive.persist(
                        experiment_id=experiment_id, snapshot=snapshot
                    )
                data = self.metrics_archive.load(experiment_id=experiment_id)
        if data is not None and not from_record and row is not None:
            with contextlib.suppress(Exception):
                self.metrics_records.record(
                    experiment_id=experiment_id,
                    project_id=str(row.get("project_id") or ""),
                    snapshot=data,
                )
        if data is None:
            return {
                "experiment_id": experiment_id,
                "available": False,
                "sandbox_status": status,
                "hint": (
                    "No archived metrics yet - they are captured from the "
                    "sandbox's MLflow on sync and right before release."
                ),
            }
        return {
            "experiment_id": experiment_id,
            "available": True,
            "sandbox_status": status,
            **data,
        }

    def persist_row(
        self,
        *,
        row: dict[str, Any],
        force: bool = False,
        snapshot: dict[str, Any] | None = None,
        snapshot_provided: bool = False,
    ) -> None:
        """Best-effort: archive the sandbox's MLflow metrics snapshot."""
        try:
            experiment_id = str(row.get("experiment_id") or "")
            if not experiment_id:
                return
            now = time.monotonic()
            last = self._persisted_at.get(experiment_id)
            if (
                not force
                and last is not None
                and now - last < METRICS_PERSIST_TTL_SECONDS
            ):
                return
            if not snapshot_provided:
                snapshot = self.worker.capture_metrics_snapshot(
                    row=row,
                    name=self.registry.experiment_name(experiment_id=experiment_id),
                )
            if not isinstance(snapshot, dict):
                return
            snapshot = dict(snapshot)
            snapshot.pop("base_url", None)
            self._persisted_at[experiment_id] = now
            path = self.metrics_archive.persist(
                experiment_id=experiment_id, snapshot=snapshot
            )
            self.metrics_records.record(
                experiment_id=experiment_id,
                project_id=str(row.get("project_id") or ""),
                snapshot=snapshot,
            )
            if force:
                self.registry.emit_event(
                    project_id=str(row.get("project_id") or ""),
                    event_type="sandbox.metrics_persisted",
                    experiment_id=experiment_id,
                    payload={
                        "sandbox_id": row.get("sandbox_id", ""),
                        "path": path.name,
                        "runs": sum(
                            len(e.get("runs") or [])
                            for e in snapshot.get("experiments") or []
                        ),
                    },
                )
        except Exception:  # noqa: BLE001 - archiving must never block callers
            return

    def sample_metrics(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Sample live in-container usage for a running sandbox."""
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id, project_id=project_id
            )
        except NotFoundError:
            return {
                "experiment_id": experiment_id,
                "status": "none",
                "available": False,
                "metrics": None,
            }
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
        metrics = self._sample_cached(
            experiment_id=experiment_id, sandbox_id=sandbox_id, row=row
        )
        return {
            **base,
            "available": metrics is not None,
            "metrics": metrics,
            "sampled_at": now_iso(),
        }

    def _sample_cached(
        self, *, experiment_id: str, sandbox_id: str, row: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(sandbox_id)
            if cached is not None and now - cached[0] < METRICS_CACHE_TTL_SECONDS:
                return cached[1]
        try:
            metrics = self.backend.sample_metrics(
                sandbox_id=sandbox_id,
                ssh_host=str(row.get("ssh_host") or ""),
                ssh_port=int(row.get("ssh_port") or 0),
                ssh_user=str(row.get("ssh_user") or ""),
                key_path=str(self.mgmt_keys.key_path(experiment_id=experiment_id)),
            )
        except Exception:  # noqa: BLE001 - metrics are best-effort
            metrics = None
        with self._lock:
            self._cache[sandbox_id] = (time.monotonic(), metrics)
        return metrics
