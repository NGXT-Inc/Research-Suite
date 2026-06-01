"""Top-level sync orchestrator.

A SyncEngine owns:
  - a per-project bounded queue (at most 1 running + 1 queued sync per project)
  - a repo-wide interprocess lock around each actual sync pass
  - the BaselineStore
  - the volume_provider (called to ensure-or-create a Modal Volume by name)
  - the SyncApplier

It exposes:
  - ensure_project_volume(project_id) — idempotent: creates the volume if it
    doesn't exist and registers the project in sync_projects.
  - sync(project_id, skip_if_busy=False) — bidirectional sync pass.

Sync is always bidirectional. There is no 'push only' or 'pull only' mode.
Conflicts are recorded in the baseline; the caller decides what to do about
them (e.g., submit checks baseline.conflict_paths() and refuses to start a
job while any path is in conflict).

Queue semantics (per project):
  state                          new caller (default)            new caller (skip_if_busy)
  ----------------------------   -----------------------------   --------------------------
  nothing running                runs now                         runs now
  running, queued slot empty     takes queued slot, waits, runs  takes queued slot, waits, runs
  running + queued (full)        coalesces onto queued slot:      skips: returns immediately
                                 wait on its event, return its    with skipped_busy=True
                                 actual result (coalesced=True)

Cross-project calls have independent in-process queues, but actual sync passes
are serialized by a repo-wide file lock because scanning, applying, and
baseline writes mutate the shared local repo.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .applier import SyncApplier
from .baseline import BaselineStore
from .scanner import local_scan, remote_scan
from .differ import three_way_diff
from .lock import InterProcessSyncLock
from .types import SyncResult


VOLUME_REPO_DIR = ""  # The volume IS the repo; no internal prefix.

ActivityHook = Callable[[str, dict[str, Any]], None]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class _SyncSlot:
    """One sync's slot in a project's queue. Holds the completion event plus
    the actual result/exception so coalescing callers receive what this sync
    actually produced — they don't manufacture a synthetic result."""

    event: threading.Event = field(default_factory=threading.Event)
    result: SyncResult | None = None
    error: BaseException | None = None


@dataclass
class _ProjectQueue:
    """Per-project bounded queue: at most one running + one queued slot."""

    guard: threading.Lock = field(default_factory=threading.Lock)
    running: _SyncSlot | None = None
    queued: _SyncSlot | None = None


class SyncEngine:
    def __init__(
        self,
        *,
        repo_root: Path,
        baseline: BaselineStore,
        volume_provider: Callable[[str], Any],
        volume_name_prefix: str = "research-plugin",
        volume_mount_path: str = "/workspace/repo",
        activity: ActivityHook | None = None,
        process_lock: InterProcessSyncLock | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.baseline = baseline
        self.volume_provider = volume_provider
        self.volume_name_prefix = volume_name_prefix
        self.volume_mount_path = volume_mount_path
        self.activity = activity
        self.process_lock = process_lock or InterProcessSyncLock(
            lock_path=self.repo_root / ".research_plugin" / "modal" / "sync.lock"
        )
        self.applier = SyncApplier(repo_root=self.repo_root, repo_dir=VOLUME_REPO_DIR)
        self._queues: dict[str, _ProjectQueue] = {}
        self._queues_guard = threading.Lock()

    # ---------- public ----------

    def volume_name(self, *, project_id: str) -> str:
        prefix = _safe_name(self.volume_name_prefix, default="research-plugin")
        suffix = _safe_name(project_id, default="default")
        return f"{prefix}-{suffix}"[:63].strip("-") or "research-plugin-default"

    def ensure_project_volume(self, *, project_id: str) -> dict[str, str]:
        """Create the Modal volume for this project if needed; register baseline row."""
        existing = self.baseline.project_info(project_id=project_id)
        if existing:
            return existing
        name = self.volume_name(project_id=project_id)
        # volume_provider is expected to be idempotent (Modal's from_name with
        # create_if_missing=True handles both create and lookup).
        self.volume_provider(name)
        self.baseline.register_project(
            project_id=project_id,
            volume_name=name,
            mount_path=self.volume_mount_path,
            repo_dir=VOLUME_REPO_DIR,
            registered_at=now_iso(),
        )
        self._emit(
            "modal.sync.volume_ready",
            {"project_id": project_id, "volume_name": name},
        )
        return {
            "project_id": project_id,
            "volume_name": name,
            "mount_path": self.volume_mount_path,
            "repo_dir": VOLUME_REPO_DIR,
        }

    def sync(
        self,
        *,
        project_id: str,
        skip_if_busy: bool = False,
    ) -> SyncResult:
        """Run a single bidirectional sync pass for project_id.

        Per project: at most 1 running + 1 queued sync.

        skip_if_busy:
          - False (default): if both slots are full, COALESCE — wait for the
            already-queued sync to complete, then return its actual result
            with coalesced=True. Use this for callers (submit, materialize)
            that need a sync to have actually happened before they proceed.
          - True: if both slots are full, return immediately with
            skipped_busy=True. Use this for the poller, which retries next tick.

        Cross-project callers have independent in-process queues, but actual
        sync passes are serialized by a repo-wide file lock.
        Conflicts are recorded in the baseline and reported in the result;
        this function does not raise on conflict. Callers that need to refuse
        on conflict (e.g. submit) check baseline.conflict_paths() after sync.
        """
        q = self._queue_for(project_id=project_id)
        my_slot: _SyncSlot | None = None
        wait_for_predecessor: threading.Event | None = None
        coalesce_slot: _SyncSlot | None = None
        role: str

        with q.guard:
            if q.running is None:
                my_slot = _SyncSlot()
                q.running = my_slot
                role = "run_now"
            elif q.queued is None:
                my_slot = _SyncSlot()
                q.queued = my_slot
                wait_for_predecessor = q.running.event
                role = "queued"
            else:
                if skip_if_busy:
                    role = "skip"
                else:
                    coalesce_slot = q.queued
                    role = "coalesce"

        if role == "skip":
            self._emit(
                "modal.sync.skipped_busy",
                {"project_id": project_id},
            )
            return SyncResult(project_id=project_id, skipped_busy=True)

        if role == "coalesce":
            assert coalesce_slot is not None
            self._emit("modal.sync.coalesced", {"project_id": project_id})
            coalesce_slot.event.wait()
            if coalesce_slot.error is not None:
                # The queued sync raised — propagate to coalescers.
                raise coalesce_slot.error
            actual = coalesce_slot.result
            if actual is None:
                # Defensive: event was set but the slot has no result.
                # Should not happen unless _do_sync_work returned None.
                return SyncResult(project_id=project_id, coalesced=True)
            # Return the queued sync's *actual* counts, marked as coalesced.
            return _with_coalesced(actual)

        if role == "queued":
            # Wait for the currently-running slot to finish. When it does,
            # the running's finally-block has already promoted us into q.running.
            assert wait_for_predecessor is not None
            wait_for_predecessor.wait()

        assert my_slot is not None
        try:
            result = self._do_sync_work_locked(
                project_id=project_id,
                skip_if_busy=skip_if_busy,
            )
            my_slot.result = result
            return result
        except BaseException as exc:
            my_slot.error = exc
            raise
        finally:
            with q.guard:
                assert q.running is my_slot
                if q.queued is not None:
                    q.running = q.queued
                    q.queued = None
                else:
                    q.running = None
            my_slot.event.set()

    def _do_sync_work_locked(self, *, project_id: str, skip_if_busy: bool) -> SyncResult:
        """Acquire the repo-wide lock before scanning/applying/baseline writes."""
        with self.process_lock.acquire(blocking=not skip_if_busy) as acquired:
            if not acquired:
                self._emit(
                    "modal.sync.skipped_busy",
                    {"project_id": project_id, "scope": "repo"},
                )
                return SyncResult(project_id=project_id, skipped_busy=True)
            return self._do_sync_work(project_id=project_id)

    def _do_sync_work(self, *, project_id: str) -> SyncResult:
        """The actual bidirectional scan + diff + apply pass. Caller holds a slot."""
        started = time.monotonic()
        info = self.ensure_project_volume(project_id=project_id)
        volume = self.volume_provider(info["volume_name"])

        local = local_scan(repo_root=self.repo_root)
        remote = remote_scan(volume=volume, repo_dir=VOLUME_REPO_DIR)
        baseline = self.baseline.load_baseline(project_id=project_id)
        conflict_paths = self.baseline.conflict_paths(project_id=project_id)

        plan = three_way_diff(
            local=local,
            remote=remote,
            baseline=baseline,
            conflict_paths=conflict_paths,
        )

        outcome = self.applier.apply(volume=volume, plan=plan)

        when = now_iso()
        for path, (local_fp, remote_fp) in outcome.fingerprints.items():
            if local_fp is None and remote_fp is None:
                self.baseline.delete_path(project_id=project_id, path=path)
            else:
                self.baseline.upsert_clean(
                    project_id=project_id,
                    path=path,
                    local=local_fp,
                    remote=remote_fp,
                    synced_at=when,
                )

        for fp in plan.converged:
            self.baseline.delete_path(project_id=project_id, path=fp.path)

        for record in plan.conflicts:
            self.baseline.mark_conflict(
                project_id=project_id,
                path=record.path,
                local=record.local,
                remote=record.remote,
                when=when,
            )
            self._emit(
                "modal.sync.conflict",
                {
                    "project_id": project_id,
                    "path": record.path,
                    "local": _fp_dict(record.local),
                    "remote": _fp_dict(record.remote),
                },
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        result = SyncResult(
            project_id=project_id,
            pushed=outcome.pushed,
            pulled=outcome.pulled,
            deleted_remote=outcome.deleted_remote,
            deleted_local=outcome.deleted_local,
            conflicts=len(plan.conflicts),
            duration_ms=duration_ms,
            skipped_conflicts=tuple(sorted(conflict_paths)),
        )
        self._emit(
            "modal.sync.pass",
            {
                "project_id": project_id,
                "pushed": result.pushed,
                "pulled": result.pulled,
                "deleted_remote": result.deleted_remote,
                "deleted_local": result.deleted_local,
                "conflicts": result.conflicts,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    # ---------- internals ----------

    def _queue_for(self, *, project_id: str) -> _ProjectQueue:
        with self._queues_guard:
            queue = self._queues.get(project_id)
            if queue is None:
                queue = _ProjectQueue()
                self._queues[project_id] = queue
            return queue

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.activity is None:
            return
        try:
            self.activity(event_type, payload)
        except Exception:  # noqa: BLE001
            pass


def _with_coalesced(result: SyncResult) -> SyncResult:
    """Return a SyncResult mirroring `result` but flagged coalesced=True."""
    return SyncResult(
        project_id=result.project_id,
        pushed=result.pushed,
        pulled=result.pulled,
        deleted_remote=result.deleted_remote,
        deleted_local=result.deleted_local,
        conflicts=result.conflicts,
        duration_ms=result.duration_ms,
        skipped_conflicts=result.skipped_conflicts,
        skipped_busy=result.skipped_busy,
        coalesced=True,
    )


def _safe_name(value: str, *, default: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return cleaned or default


def _fp_dict(fp: Any) -> dict[str, Any] | None:
    if fp is None:
        return None
    return {
        "path": fp.path,
        "mtime_ns": fp.mtime_ns,
        "size_bytes": fp.size_bytes,
    }
