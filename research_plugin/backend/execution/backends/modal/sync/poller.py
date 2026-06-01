"""Background polling thread.

Iterates known projects every interval_seconds and runs a bidirectional sync.
Survives errors; the thread does not die on a failed pass.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Callable

from .baseline import BaselineStore
from .engine import SyncEngine


ActivityHook = Callable[[str, dict[str, Any]], None]
ShouldSyncProject = Callable[[str], bool]


class SyncPoller:
    def __init__(
        self,
        *,
        engine: SyncEngine,
        baseline: BaselineStore,
        interval_seconds: float = 60.0,
        activity: ActivityHook | None = None,
        should_sync_project: ShouldSyncProject | None = None,
    ) -> None:
        self.engine = engine
        self.baseline = baseline
        self.interval_seconds = float(interval_seconds)
        self.activity = activity
        self.should_sync_project = should_sync_project
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._loop,
            name="modal-sync-poller",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        # Wait one interval before first poll so submit-time sync covers fresh boot.
        if self._stop.wait(self.interval_seconds):
            return
        while not self._stop.is_set():
            self._tick()
            if self._stop.wait(self.interval_seconds):
                return

    def _tick(self) -> None:
        try:
            project_ids = self.baseline.known_projects()
        except Exception as exc:  # noqa: BLE001
            self._emit(
                "modal.sync.error", {"phase": "poll_list_projects", "message": str(exc)}
            )
            return
        for project_id in project_ids:
            if self._stop.is_set():
                return
            try:
                if (
                    self.should_sync_project is not None
                    and not self.should_sync_project(project_id)
                ):
                    self._emit(
                        "modal.sync.skipped_project_gate", {"project_id": project_id}
                    )
                    self.baseline.mark_polled(project_id=project_id, when=_now_iso())
                    continue
                self.engine.sync(project_id=project_id, skip_if_busy=True)
                # Update poll-clock even when we skipped — it records that we
                # checked, not that we transferred. (engine already emitted
                # modal.sync.skipped_busy / .coalesced as appropriate.)
                self.baseline.mark_polled(project_id=project_id, when=_now_iso())
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    "modal.sync.error",
                    {
                        "phase": "poll_sync",
                        "project_id": project_id,
                        "message": str(exc),
                    },
                )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.activity is None:
            return
        try:
            self.activity(event_type, payload)
        except Exception:  # noqa: BLE001
            pass


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
