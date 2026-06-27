"""Background sandbox daemons: expiration plus idle reaping."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from typing import Any, Callable

from ...sandbox.sandbox_backend import SandboxBackend
from ...env import env_bool, env_float
from ...ports.sandbox_lifecycle import ProvisionReaper
from .sandbox_registry import SandboxRegistry
from ...sandbox.sandbox_support import (
    DEFAULT_REAPER_INTERVAL_SECONDS,
    DEFAULT_SANDBOX_IDLE_SECONDS,
    DEFAULT_STALE_PROVISION_DEADLINE_SECONDS,
    parse_iso,
)
from .sandbox_heartbeat import SandboxHeartbeatMonitor, SandboxIdlePolicy


class SandboxDaemons:
    """Owns control-plane background loops and composes their policies."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        provisioner: ProvisionReaper,
        sample_metrics: Callable[..., dict[str, Any]] | None = None,
        idle_policy: SandboxIdlePolicy | None = None,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.provisioner = provisioner
        self.heartbeat = SandboxHeartbeatMonitor(
            registry=registry,
            sample_metrics=sample_metrics or (lambda **_kwargs: {}),
            reap_row=self._reap_row,
            policy=idle_policy,
        )
        self._reaper_stop = threading.Event()
        self.reaper_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._daemon_enabled():
            self.reaper_thread = threading.Thread(
                target=self._reaper_loop,
                name="sandbox-reaper",
                daemon=True,
            )
            self.reaper_thread.start()

    def stop(self) -> None:
        self._reaper_stop.set()
        if self.reaper_thread is not None:
            self.reaper_thread.join(timeout=2.0)

    # ---------- expiration reaper ----------

    def _daemon_enabled(self) -> bool:
        return self._reaper_enabled() or self._idle_reap_threshold() > 0

    def _reaper_enabled(self) -> bool:
        # Cost governance (cloud plan Phase 7): in CONTROL mode the env
        # off-switch is IGNORED — the cloud holds the provider keys and pays for
        # every VM, so an operator-set RESEARCH_PLUGIN_SANDBOX_REAPER=0 must not
        # be able to leave billing VMs unreaped. Local/daemon mode keeps the
        # switch (the user owns their own bill). enforce_expiry still gates by
        # backend (the in-memory fake opts out).
        from ...config import Mode, resolve_mode

        if resolve_mode() is not Mode.CONTROL:
            if not env_bool("RESEARCH_PLUGIN_SANDBOX_REAPER", default=True):
                return False
        return self.backend.capabilities.enforce_expiry

    def _reaper_loop(self) -> None:
        interval = env_float(
            "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL",
            None,
            DEFAULT_REAPER_INTERVAL_SECONDS,
        )
        stale_deadline = env_float(
            "RESEARCH_PLUGIN_SANDBOX_STALE_PROVISION_DEADLINE",
            None,
            DEFAULT_STALE_PROVISION_DEADLINE_SECONDS,
        )
        while not self._reaper_stop.wait(interval):
            expiry_enabled = self._reaper_enabled()
            try:
                if expiry_enabled:
                    self.reap_expired()
            except Exception:  # noqa: BLE001 — the reaper must never die
                pass
            try:
                self.reap_idle(threshold_seconds=self._idle_reap_threshold())
            except Exception:  # noqa: BLE001 — the reaper must never die
                pass
            # The reaper handles `running` rows by expires_at; a provision that
            # wedged before reaching `running` (daemon crash mid-provision) has
            # no expires_at, so without this its billing VM would leak until the
            # agent happened to re-poll. In local mode this thread is the only
            # proactive billing backstop (CleanupService runs only in the cloud).
            try:
                if expiry_enabled:
                    self.provisioner.reap_stale_provisions(
                        now=datetime.now(tz=UTC), deadline_seconds=stale_deadline
                    )
            except Exception:  # noqa: BLE001 — the reaper must never die
                pass

    def _idle_reap_threshold(self) -> float:
        raw = os.environ.get("RESEARCH_PLUGIN_SANDBOX_IDLE_SECONDS")
        if raw is not None and raw.strip() == "":
            return 0.0
        threshold = env_float(
            "RESEARCH_PLUGIN_SANDBOX_IDLE_SECONDS",
            None,
            DEFAULT_SANDBOX_IDLE_SECONDS,
        )
        return threshold if threshold > 0 else 0.0

    def reap_expired(self, *, now: datetime | None = None) -> int:
        """Terminate every running sandbox whose expires_at deadline has passed.

        Idempotent and safe to call directly (tests do). Returns how many were
        reaped.
        """
        now_dt = now or datetime.now(tz=UTC)
        reaped = 0
        for row in self.registry.list_running_rows():
            expires_at = parse_iso(row.get("expires_at"))
            if expires_at is None or now_dt < expires_at:
                continue
            self._reap_row(row=row)
            reaped += 1
        return reaped

    def reap_idle(
        self,
        *,
        now: datetime | None = None,
        threshold_seconds: float | None = None,
    ) -> int:
        return self.heartbeat.reap_idle(
            now=now,
            threshold_seconds=(
                self._idle_reap_threshold()
                if threshold_seconds is None
                else float(threshold_seconds)
            ),
        )

    def _reap_row(
        self,
        *,
        row: dict[str, Any],
        event_type: str = "sandbox.expired",
        payload_extra: dict[str, Any] | None = None,
    ) -> None:
        experiment_id = str(row.get("experiment_id") or "")
        sandbox_id = str(row.get("sandbox_id") or "")
        stopped = False
        if sandbox_id:
            try:
                stopped = self.backend.terminate(sandbox_id=sandbox_id)
            except Exception:  # noqa: BLE001
                stopped = False
        if not stopped:
            self.provisioner.cleanup_orphan(experiment_id=experiment_id, row=row)
        sandbox_uid = str(row.get("sandbox_uid") or "")
        self.registry.mark_terminated(
            experiment_id=experiment_id, sandbox_uid=sandbox_uid
        )
        payload = {
            "sandbox_id": sandbox_id,
            "sandbox_uid": sandbox_uid,
            "reaped": True,
            "expires_at": row.get("expires_at"),
            "stopped": stopped,
        }
        if payload_extra:
            payload.update(payload_extra)
        self.registry.emit_event(
            project_id=str(row.get("project_id")),
            event_type=event_type,
            experiment_id=experiment_id,
            payload=payload,
        )
