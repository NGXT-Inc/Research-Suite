"""Background sandbox daemons: expiration plus idle reaping.

Pure scheduling: the loops decide *when* to sweep; every terminate/mark
decision belongs to `SandboxLifecycle` (expiry + idle rows) and the
provisioner's stale-provision reaper (wedged pre-running rows).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Callable

from ...sandbox.sandbox_backend import SandboxBackend
from ...env import env_bool, env_float, env_raw
from ...ports.sandbox_lifecycle import ProvisionReaper
from .sandbox_lifecycle import SandboxLifecycle
from .sandbox_registry import SandboxRegistry
from ...sandbox.sandbox_support import (
    DEFAULT_REAPER_INTERVAL_SECONDS,
    DEFAULT_SANDBOX_IDLE_SECONDS,
    DEFAULT_STALE_PROVISION_DEADLINE_SECONDS,
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
        lifecycle: SandboxLifecycle,
        sample_metrics: Callable[..., dict[str, Any]] | None = None,
        reconcile_runs: Callable[[], int] | None = None,
        idle_policy: SandboxIdlePolicy | None = None,
        force_expiry_reaper: bool = False,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.provisioner = provisioner
        self.lifecycle = lifecycle
        # rp_run observation piggybacks the existing sweep cadence: each pass
        # mirrors .runs receipts for live sandboxes and emits run.finished.
        self.reconcile_runs = reconcile_runs
        # Cost governance (cloud plan Phase 7): the hosted control composition
        # passes True — the cloud holds the provider keys and pays for every
        # VM, so an operator-set RESEARCH_PLUGIN_SANDBOX_REAPER=0 must not be
        # able to leave billing VMs unreaped. Local/daemon compositions pass
        # False and keep the env off-switch (the user owns their own bill).
        self.force_expiry_reaper = bool(force_expiry_reaper)
        self.heartbeat = SandboxHeartbeatMonitor(
            registry=registry,
            sample_metrics=sample_metrics or (lambda **_kwargs: {}),
            reap_row=lifecycle.reap_row,
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
        # With force_expiry_reaper (hosted control) the env off-switch is
        # IGNORED; otherwise it is honored. enforce_expiry still gates by
        # backend (the in-memory fake opts out).
        if not self.force_expiry_reaper:
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
                    self.lifecycle.reap_expired()
            except Exception:  # noqa: BLE001 — the reaper must never die
                pass
            try:
                self.reap_idle(threshold_seconds=self._idle_reap_threshold())
            except Exception:  # noqa: BLE001 — the reaper must never die
                pass
            try:
                if self.reconcile_runs is not None:
                    self.reconcile_runs()
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
        raw = env_raw("MERV_SANDBOX_IDLE_SECONDS")
        if raw == "":
            return 0.0
        threshold = env_float(
            "RESEARCH_PLUGIN_SANDBOX_IDLE_SECONDS",
            None,
            DEFAULT_SANDBOX_IDLE_SECONDS,
        )
        return threshold if threshold > 0 else 0.0

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

