"""Live sandbox usage sampling kept out of the sandbox facade."""

from __future__ import annotations

import threading
import time
from typing import Any

from ...ports.mgmt_keys import MgmtKeyStore
from ...sandbox.sandbox_backend import SandboxBackend
from ...sandbox.sandbox_support import (
    ACTIVE_SANDBOX_STATUSES,
    METRICS_CACHE_TTL_SECONDS,
)
from ...utils import NotFoundError, now_iso
from .sandbox_registry import SandboxRegistry


class SandboxMetrics:
    """Best-effort live usage metrics for running sandbox rows."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        mgmt_keys: MgmtKeyStore,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.mgmt_keys = mgmt_keys
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._lock = threading.Lock()

    def sample_metrics(
        self,
        *,
        experiment_id: str,
        project_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        """Sample live in-container usage for a running sandbox."""
        try:
            row = self.registry.fetch_scoped(
                experiment_id=experiment_id,
                project_id=project_id,
                sandbox_uid=sandbox_uid,
            )
        except NotFoundError:
            return {
                "experiment_id": experiment_id,
                "sandbox_uid": sandbox_uid or "",
                "status": "none",
                "available": False,
                "metrics": None,
            }
        resolved_experiment_id = experiment_id or str(row.get("experiment_id") or "")
        status = row.get("status")
        sandbox_id = str(row.get("sandbox_id") or "")
        base: dict[str, Any] = {
            "experiment_id": resolved_experiment_id,
            "sandbox_uid": row.get("sandbox_uid", ""),
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
            experiment_id=resolved_experiment_id, sandbox_id=sandbox_id, row=row
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
                key_path=str(self._mgmt_key_path(row=row)),
            )
        except Exception:  # noqa: BLE001 - metrics are best-effort
            metrics = None
        with self._lock:
            self._cache[sandbox_id] = (time.monotonic(), metrics)
        return metrics

    def _mgmt_key_path(self, *, row: dict[str, Any]) -> Any:
        return self.mgmt_keys.key_path(sandbox_uid=str(row.get("sandbox_uid") or ""))
