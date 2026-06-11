"""Background sandbox daemons: the auto-rsync poller and the expiration reaper.

`SandboxDaemons` owns the two long-lived threads and their policy (enable
gates, intervals, the reap composition). It reads rows from `SandboxRegistry`,
terminates via the backend, cleans orphans via the provisioner, reverts
experiments only through the workflow engine's system transitions, and reaches
the facade through two injected callables — ``sync_row`` and
``persist_metrics`` — so the facade's swappable rsync syncer and metrics
archive stay where they are.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from typing import Any, Callable

from ..execution.types import SandboxBackend
from .experiments import ExperimentService
from .sandbox_provisioner import SandboxProvisioner
from .sandbox_registry import SandboxRegistry
from .sandbox_support import (
    DEFAULT_AUTO_RSYNC_INTERVAL_SECONDS,
    DEFAULT_REAPER_INTERVAL_SECONDS,
    env_float,
    parse_iso,
)


class SandboxDaemons:
    """Owns the auto-sync and reaper threads plus the reap policy."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        provisioner: SandboxProvisioner,
        experiments: ExperimentService,
        sync_row: Callable[..., dict[str, Any]],
        persist_metrics: Callable[..., None],
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.provisioner = provisioner
        self.experiments = experiments
        self._sync_row = sync_row
        self._persist_metrics = persist_metrics
        self._auto_sync_stop = threading.Event()
        self._reaper_stop = threading.Event()
        self.auto_sync_thread: threading.Thread | None = None
        self.reaper_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._auto_sync_enabled():
            self.auto_sync_thread = threading.Thread(
                target=self._auto_sync_loop,
                name="sandbox-rsync-poller",
                daemon=True,
            )
            self.auto_sync_thread.start()
        if self._reaper_enabled():
            self.reaper_thread = threading.Thread(
                target=self._reaper_loop,
                name="sandbox-reaper",
                daemon=True,
            )
            self.reaper_thread.start()

    def stop(self) -> None:
        self._auto_sync_stop.set()
        self._reaper_stop.set()
        if self.auto_sync_thread is not None:
            self.auto_sync_thread.join(timeout=2.0)
        if self.reaper_thread is not None:
            self.reaper_thread.join(timeout=2.0)

    # ---------- expiration reaper ----------

    def _reaper_enabled(self) -> bool:
        raw = os.environ.get("RESEARCH_PLUGIN_SANDBOX_REAPER", "1").lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        return self.backend.capabilities.enforce_expiry

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
        for row in self.registry.list_running_rows():
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
        self._persist_metrics(row=row, force=True)
        sandbox_id = str(row.get("sandbox_id") or "")
        stopped = False
        if sandbox_id:
            try:
                stopped = self.backend.terminate(sandbox_id=sandbox_id)
            except Exception:  # noqa: BLE001
                stopped = False
        self.provisioner.cleanup_orphan(experiment_id=experiment_id, row=row)
        self.registry.mark_terminated(experiment_id=experiment_id)
        # An experiment whose sandbox expired underneath it must not stay
        # 'running' forever; ready_to_run is truthful (nothing is executing)
        # and lets the agent simply request a fresh sandbox. The system
        # transition no-ops for experiments already past running.
        reverted = self.experiments.apply_system_transition(
            experiment_id=experiment_id,
            transition="sandbox_expired",
            reason="sandbox reaped at expires_at deadline",
        )
        self.registry.emit_event(
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

    # ---------- auto rsync poller ----------

    def _auto_sync_enabled(self) -> bool:
        raw = os.environ.get("RESEARCH_PLUGIN_SANDBOX_AUTO_RSYNC", "1").lower()
        if raw in {"0", "false", "no", "off"}:
            return False
        return self.backend.capabilities.auto_sync

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
            rows = self.registry.list_running_rows()
            active = {str(row.get("experiment_id")) for row in rows}
            for gone in set(last_errors) - active:
                last_errors.pop(gone, None)
            for row in rows:
                experiment_id = str(row.get("experiment_id"))
                try:
                    result = self._sync_row(row=row, skip_if_busy=True)
                    if not result.get("skipped"):
                        last_errors.pop(experiment_id, None)
                        self._persist_metrics(row=row)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    if last_errors.get(experiment_id) == message:
                        continue  # already reported this exact failure
                    last_errors[experiment_id] = message
                    self.registry.emit_event(
                        project_id=str(row.get("project_id")),
                        event_type="sandbox.rsync_error",
                        experiment_id=experiment_id,
                        payload={"error": message, "sandbox_id": row.get("sandbox_id", "")},
                    )
