"""Background sandbox daemons: the auto-rsync poller and the expiration reaper.

`SandboxDaemons` owns the two long-lived threads and their policy (enable
gates, intervals, the reap composition). The reaper — control-plane work that
moves cloud-side in split mode — reads rows from `SandboxRegistry` directly,
terminates via the backend, cleans orphans via the provisioner, and reverts
experiments only through the workflow engine's system transitions. The
auto-sync poller — data-plane work that stays on the user's machine — never
reads rows directly: it asks the injected `ControlPlaneView` for "my running
sandboxes + a sync lease for each" (cloud plan Phase 4), the exact call that
becomes the daemon's HTTP poll in Phase 8. Both loops reach the facade
through injected callables — ``sync_row``, ``final_pull``,
``persist_metrics``, and ``parachute`` — so the facade's swappable rsync
syncer, metrics archive, and blob store stay where they are.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Callable

from ...sandbox_backend import SandboxBackend
from ...sandbox_autosync import run_auto_sync_target
from ...env import env_bool, env_float
from ...ports.sandbox_lifecycle import ExperimentTransitions, ProvisionReaper
from ...ports.sandbox_sync import ControlPlaneView
from .sandbox_registry import SandboxRegistry
from ...sandbox_support import (
    DEFAULT_AUTO_RSYNC_INTERVAL_SECONDS,
    DEFAULT_REAPER_INTERVAL_SECONDS,
    DEFAULT_STALE_PROVISION_DEADLINE_SECONDS,
    parse_iso,
)


class SandboxDaemons:
    """Owns the auto-sync and reaper threads plus the reap policy."""

    def __init__(
        self,
        *,
        registry: SandboxRegistry,
        backend: SandboxBackend,
        provisioner: ProvisionReaper,
        experiments: ExperimentTransitions,
        control_view: ControlPlaneView,
        sync_row: Callable[..., dict[str, Any]],
        final_pull: Callable[..., dict[str, Any]],
        persist_metrics: Callable[..., None],
        parachute: Callable[..., None],
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.provisioner = provisioner
        self.experiments = experiments
        self.view = control_view
        self._sync_row = sync_row
        self._final_pull = final_pull
        self._persist_metrics = persist_metrics
        self._parachute = parachute
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
        # Cost governance (cloud plan Phase 7): in CONTROL mode the env
        # off-switch is IGNORED — the cloud holds the provider keys and pays for
        # every VM, so an operator-set RESEARCH_PLUGIN_SANDBOX_REAPER=0 must not
        # be able to leave billing VMs unreaped. Local/daemon mode keeps the
        # switch (the user owns their own bill). enforce_expiry still gates by
        # backend (the in-memory fake opts out).
        from ...config import resolve_auth_required

        if not resolve_auth_required():
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
            try:
                self.reap_expired()
            except Exception:  # noqa: BLE001 — the reaper must never die
                pass
            # The reaper handles `running` rows by expires_at; a provision that
            # wedged before reaching `running` (daemon crash mid-provision) has
            # no expires_at, so without this its billing VM would leak until the
            # agent happened to re-poll. In local mode this thread is the only
            # proactive billing backstop (CleanupService runs only in the cloud).
            try:
                self.provisioner.reap_stale_provisions(
                    now=datetime.now(tz=UTC), deadline_seconds=stale_deadline
                )
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
        # Preserve outputs before the kill — same courtesy as release(). The
        # pull arrives at the worker as a final_pull task with a deadline
        # (plan Phase 4); when it fails — daemon unreachable or rsync broken
        # — the parachute rescues the experiment dir over the management
        # channel instead (plan Phase 5, fixed decision 5). In-process the
        # local worker is by definition reachable, so the branch is
        # exercised by injecting a failing final_pull; parachute() itself
        # never raises (loud sandbox.parachute_failed event) and reaping
        # always proceeds to terminate — billing protection comes first.
        final_result: dict[str, Any] = {}
        try:
            final_result = self._final_pull(row=row)
        except Exception:  # noqa: BLE001 — reaping must still terminate
            self._parachute(row=row)
        self._persist_metrics(
            row=row,
            force=True,
            snapshot=final_result.get("metrics_snapshot"),
            snapshot_provided=True,
        )
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
        # Config matrix (cloud plan §3.4): control mode runs the reaper but NOT
        # the auto-rsync poller — the cloud never rsyncs a user checkout (the
        # daemon owns that). In local/daemon mode the env switch + the backend
        # capability decide as before.
        from ...config import Mode, resolve_mode

        if resolve_mode() is Mode.CONTROL:
            return False
        if not env_bool("RESEARCH_PLUGIN_SANDBOX_AUTO_RSYNC", default=True):
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
            # Targets come from the ControlPlaneView — running sandboxes with
            # a lease granted/renewed for this client (plan Phase 4). A row
            # another client is syncing is simply absent here.
            targets = self.view.sync_targets()
            active = {
                str(target["row"].get("experiment_id")) for target in targets
            }
            for gone in set(last_errors) - active:
                last_errors.pop(gone, None)
            for target in targets:
                row = target["row"]
                experiment_id = str(row.get("experiment_id"))
                try:
                    result, _ = run_auto_sync_target(
                        target=target,
                        sync_pull=self._sync_row,
                        sync_includes_row=True,
                        after_sync=self._persist_metrics,
                    )
                    if not result.get("skipped"):
                        last_errors.pop(experiment_id, None)
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
