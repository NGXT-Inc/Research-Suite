"""Unit tests for the modal sync subsystem.

These exercise the three-way differ, scanner exclusions, baseline store, and
the engine's locking/skip-if-busy semantics. End-to-end backend integration is
covered by test_modal_backend.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.execution.backends.modal.sync.baseline import BaselineStore
from backend.execution.backends.modal.sync.differ import three_way_diff
from backend.execution.backends.modal.sync.engine import SyncEngine
from backend.execution.backends.modal.sync.lock import InterProcessSyncLock
from backend.execution.backends.modal.sync.poller import SyncPoller
from backend.execution.backends.modal.sync.scanner import local_scan
from backend.execution.backends.modal.sync.types import (
    ConflictRecord,
    FileFingerprint,
    SyncResult,
)


def fp(path: str, mtime: int, size: int) -> FileFingerprint:
    return FileFingerprint(path=path, mtime_ns=mtime, size_bytes=size)


class ThreeWayDiffTest(unittest.TestCase):
    def test_local_changed_only_pushes(self) -> None:
        local = {"a.txt": fp("a.txt", 2, 10)}
        remote = {"a.txt": fp("a.txt", 1, 10)}
        baseline = {"a.txt": (fp("a.txt", 1, 10), fp("a.txt", 1, 10))}
        plan = three_way_diff(local=local, remote=remote, baseline=baseline)
        self.assertEqual(len(plan.push), 1)
        self.assertEqual(plan.push[0].path, "a.txt")
        self.assertEqual(plan.pull, ())
        self.assertEqual(plan.conflicts, ())

    def test_remote_changed_only_pulls(self) -> None:
        local = {"a.txt": fp("a.txt", 1, 10)}
        remote = {"a.txt": fp("a.txt", 9, 99)}
        baseline = {"a.txt": (fp("a.txt", 1, 10), fp("a.txt", 1, 10))}
        plan = three_way_diff(local=local, remote=remote, baseline=baseline)
        self.assertEqual(len(plan.pull), 1)
        self.assertEqual(plan.pull[0].path, "a.txt")
        self.assertEqual(plan.push, ())

    def test_new_local_file_pushes(self) -> None:
        local = {"new.txt": fp("new.txt", 5, 5)}
        remote: dict[str, FileFingerprint] = {}
        baseline: dict[str, tuple] = {}
        plan = three_way_diff(local=local, remote=remote, baseline=baseline)
        self.assertEqual([f.path for f in plan.push], ["new.txt"])

    def test_new_remote_file_pulls(self) -> None:
        remote = {"new.txt": fp("new.txt", 5, 5)}
        local: dict[str, FileFingerprint] = {}
        baseline: dict[str, tuple] = {}
        plan = three_way_diff(local=local, remote=remote, baseline=baseline)
        self.assertEqual([f.path for f in plan.pull], ["new.txt"])

    def test_local_only_deletion_deletes_remote(self) -> None:
        local: dict[str, FileFingerprint] = {}
        remote = {"x.txt": fp("x.txt", 1, 1)}
        baseline = {"x.txt": (fp("x.txt", 1, 1), fp("x.txt", 1, 1))}
        plan = three_way_diff(local=local, remote=remote, baseline=baseline)
        self.assertEqual(plan.delete_remote, ("x.txt",))
        self.assertEqual(plan.delete_local, ())

    def test_both_sides_changed_marks_conflict(self) -> None:
        local = {"a.txt": fp("a.txt", 9, 99)}
        remote = {"a.txt": fp("a.txt", 7, 77)}
        baseline = {"a.txt": (fp("a.txt", 1, 10), fp("a.txt", 1, 10))}
        plan = three_way_diff(local=local, remote=remote, baseline=baseline)
        self.assertEqual(plan.push, ())
        self.assertEqual(plan.pull, ())
        self.assertEqual(len(plan.conflicts), 1)
        record: ConflictRecord = plan.conflicts[0]
        self.assertEqual(record.path, "a.txt")
        self.assertEqual(record.local, fp("a.txt", 9, 99))
        self.assertEqual(record.remote, fp("a.txt", 7, 77))

    def test_known_conflicts_are_skipped(self) -> None:
        local = {"a.txt": fp("a.txt", 9, 99)}
        remote = {"a.txt": fp("a.txt", 7, 77)}
        baseline = {"a.txt": (fp("a.txt", 1, 10), fp("a.txt", 1, 10))}
        plan = three_way_diff(
            local=local,
            remote=remote,
            baseline=baseline,
            conflict_paths={"a.txt"},
        )
        self.assertTrue(plan.is_empty())

    def test_unchanged_path_is_skipped(self) -> None:
        local = {"a.txt": fp("a.txt", 1, 10)}
        remote = {"a.txt": fp("a.txt", 1, 10)}
        baseline = {"a.txt": (fp("a.txt", 1, 10), fp("a.txt", 1, 10))}
        plan = three_way_diff(local=local, remote=remote, baseline=baseline)
        self.assertTrue(plan.is_empty())


class LocalScanExclusionsTest(unittest.TestCase):
    def test_scan_excludes_state_git_venv_pycache_and_pyc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "main.py").write_text("ok\n")
            (repo / "src" / "cached.pyc").write_text("bytecode\n")
            (repo / "__pycache__").mkdir()
            (repo / "__pycache__" / "a.pyc").write_text("x\n")
            (repo / ".git").mkdir()
            (repo / ".git" / "HEAD").write_text("ref\n")
            (repo / ".venv").mkdir()
            (repo / ".venv" / "python").write_text("\n")
            (repo / ".research_plugin").mkdir()
            (repo / ".research_plugin" / "state.sqlite").write_text("\n")
            (repo / ".research_plugin_job").mkdir()
            (repo / ".research_plugin_job" / "status.json").write_text("{}\n")

            result = local_scan(repo_root=repo)
            self.assertIn("src/main.py", result)
            self.assertNotIn("src/cached.pyc", result)
            self.assertFalse(any(p.startswith("__pycache__/") for p in result))
            self.assertFalse(any(p.startswith(".git/") for p in result))
            self.assertFalse(any(p.startswith(".venv/") for p in result))
            self.assertFalse(any(p.startswith(".research_plugin/") for p in result))
            self.assertFalse(any(p.startswith(".research_plugin_job/") for p in result))


class BaselineStoreTest(unittest.TestCase):
    def test_upsert_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BaselineStore(db_path=Path(tmp) / "sync.sqlite")
            store.upsert_clean(
                project_id="p1",
                path="a.txt",
                local=fp("a.txt", 1, 10),
                remote=fp("a.txt", 2, 10),
                synced_at="2026-01-01T00:00:00Z",
            )
            baseline = store.load_baseline(project_id="p1")
            self.assertEqual(
                baseline["a.txt"],
                (fp("a.txt", 1, 10), fp("a.txt", 2, 10)),
            )

    def test_conflicts_are_excluded_from_baseline_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BaselineStore(db_path=Path(tmp) / "sync.sqlite")
            store.upsert_clean(
                project_id="p1",
                path="ok.txt",
                local=fp("ok.txt", 1, 1),
                remote=fp("ok.txt", 1, 1),
                synced_at="2026-01-01T00:00:00Z",
            )
            store.mark_conflict(
                project_id="p1",
                path="bad.txt",
                local=fp("bad.txt", 5, 5),
                remote=fp("bad.txt", 6, 6),
                when="2026-01-01T00:00:01Z",
            )
            baseline = store.load_baseline(project_id="p1")
            self.assertIn("ok.txt", baseline)
            self.assertNotIn("bad.txt", baseline)
            self.assertEqual(store.conflict_paths(project_id="p1"), {"bad.txt"})

    def test_known_projects_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BaselineStore(db_path=Path(tmp) / "sync.sqlite")
            self.assertEqual(store.known_projects(), [])
            store.register_project(
                project_id="proj_a",
                volume_name="research-plugin-proj_a",
                mount_path="/workspace/repo",
                repo_dir="",
                registered_at="2026-01-01T00:00:00Z",
            )
            self.assertEqual(store.known_projects(), ["proj_a"])
            info = store.project_info(project_id="proj_a")
            assert info is not None
            self.assertEqual(info["volume_name"], "research-plugin-proj_a")


class _StubVolume:
    """Minimal modal-volume stub that satisfies SyncEngine + scanner."""

    def listdir(self, _path: str, recursive: bool = True):  # noqa: ARG002
        return []


class SyncEngineLockingTest(unittest.TestCase):
    """Concurrency semantics of SyncEngine.sync.

    Strategy: install a hold-the-lock-then-release artifact by patching the
    engine's applier so we can pause a 'first' sync mid-flight from another
    thread, then assert how a 'second' sync behaves (queues vs. skips).
    """

    def _engine(self, tmp: Path) -> SyncEngine:
        repo = tmp / "repo"
        repo.mkdir()
        baseline = BaselineStore(db_path=tmp / "sync.sqlite")
        volume = _StubVolume()
        return SyncEngine(
            repo_root=repo,
            baseline=baseline,
            volume_provider=lambda _name: volume,
        )

    def _pause_applier(self, engine: SyncEngine) -> threading.Event:
        """Patch engine.applier.apply to block on this Event before returning."""
        gate = threading.Event()
        original_apply = engine.applier.apply

        def slow_apply(*args, **kwargs):
            result = original_apply(*args, **kwargs)
            gate.wait(timeout=5.0)
            return result

        engine.applier.apply = slow_apply  # type: ignore[assignment]
        return gate

    def test_skip_if_busy_skips_only_when_both_slots_full(self) -> None:
        """skip_if_busy=True returns immediately ONLY when running+queued
        are both occupied. With only the running slot taken, the caller
        should take the queued slot and wait its turn (no skip)."""
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp))
            gate = self._pause_applier(engine)

            errors: list[BaseException] = []
            sync1_entered = threading.Event()
            sync2_entered = threading.Event()

            def sync1() -> None:
                try:
                    sync1_entered.set()
                    engine.sync(project_id="p1")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def sync2() -> None:
                # Default caller — takes the queued slot, waits, then runs.
                try:
                    sync2_entered.set()
                    engine.sync(project_id="p1")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=sync1, daemon=True)
            t1.start()
            self.assertTrue(sync1_entered.wait(timeout=2.0))
            time.sleep(0.05)  # let sync1 reach apply() and block on the gate

            t2 = threading.Thread(target=sync2, daemon=True)
            t2.start()
            self.assertTrue(sync2_entered.wait(timeout=2.0))
            time.sleep(0.05)  # let sync2 take the queued slot and block

            # Now: sync1 holds running, sync2 holds queued. Both slots full.
            # A third caller with skip_if_busy=True must return immediately.
            start = time.monotonic()
            result = engine.sync(
                project_id="p1",
                skip_if_busy=True,
            )
            elapsed = time.monotonic() - start

            self.assertTrue(result.skipped_busy)
            self.assertFalse(result.coalesced)
            self.assertLess(elapsed, 0.5)

            gate.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            self.assertEqual(errors, [])

    def test_default_caller_coalesces_when_both_slots_full(self) -> None:
        """When both slots are full, a default-policy caller waits for the
        already-queued sync to complete and returns coalesced=True."""
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp))
            gate = self._pause_applier(engine)

            errors: list[BaseException] = []
            sync1_entered = threading.Event()
            sync2_entered = threading.Event()

            def sync1() -> None:
                try:
                    sync1_entered.set()
                    engine.sync(project_id="p1")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def sync2() -> None:
                try:
                    sync2_entered.set()
                    engine.sync(project_id="p1")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=sync1, daemon=True)
            t1.start()
            self.assertTrue(sync1_entered.wait(timeout=2.0))
            time.sleep(0.05)
            t2 = threading.Thread(target=sync2, daemon=True)
            t2.start()
            self.assertTrue(sync2_entered.wait(timeout=2.0))
            time.sleep(0.05)

            # Both slots full. Default caller should coalesce: wait for the
            # queued sync to finish, then return without running its own work.
            coalesce_result: list = []

            def sync3() -> None:
                coalesce_result.append(
                    engine.sync(project_id="p1")
                )

            t3 = threading.Thread(target=sync3, daemon=True)
            t3.start()
            # Verify sync3 is genuinely blocked while the queue is full.
            time.sleep(0.1)
            self.assertTrue(t3.is_alive())

            gate.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            t3.join(timeout=5.0)
            self.assertEqual(errors, [])
            self.assertEqual(len(coalesce_result), 1)
            self.assertTrue(coalesce_result[0].coalesced)
            self.assertFalse(coalesce_result[0].skipped_busy)

    def test_coalescer_receives_queued_sync_actual_result(self) -> None:
        """The coalesce path returns the queued sync's *actual* SyncResult
        (with non-zero counts when the queued sync did work), plus
        coalesced=True. The coalescer's request literally becomes the queued
        one — same result object's counts."""
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp))

            fake_result = SyncResult(
                project_id="p1",
                pushed=7,
                pulled=3,
                deleted_remote=1,
                deleted_local=2,
                conflicts=0,
                duration_ms=42,
            )
            first_entered = threading.Event()
            release = threading.Event()
            calls = []

            def fake_work(*, project_id: str):
                calls.append(project_id)
                if len(calls) == 1:
                    first_entered.set()
                    release.wait(timeout=5.0)
                return fake_result

            engine._do_sync_work = fake_work  # type: ignore[assignment]

            # Spin up running + queued + coalescing caller.
            running_t = threading.Thread(
                target=lambda: engine.sync(project_id="p1"),
                daemon=True,
            )
            running_t.start()
            self.assertTrue(first_entered.wait(timeout=2.0))

            queued_t = threading.Thread(
                target=lambda: engine.sync(project_id="p1"),
                daemon=True,
            )
            queued_t.start()
            time.sleep(0.05)  # let queued_t take the queued slot

            coalesce_results: list = []

            def coalescer():
                coalesce_results.append(engine.sync(project_id="p1"))

            coalesce_t = threading.Thread(target=coalescer, daemon=True)
            coalesce_t.start()
            time.sleep(0.05)  # let it reach the coalesce wait

            release.set()
            running_t.join(timeout=5.0)
            queued_t.join(timeout=5.0)
            coalesce_t.join(timeout=5.0)

            self.assertEqual(len(coalesce_results), 1)
            r = coalesce_results[0]
            # Coalescer gets the actual counts from the queued sync (not zeros).
            self.assertEqual(r.pushed, 7)
            self.assertEqual(r.pulled, 3)
            self.assertEqual(r.deleted_remote, 1)
            self.assertEqual(r.deleted_local, 2)
            self.assertEqual(r.duration_ms, 42)
            self.assertTrue(r.coalesced)
            self.assertFalse(r.skipped_busy)

    def test_at_most_one_caller_queued_among_many_default_callers(self) -> None:
        """Five concurrent default-policy callers → exactly two actually run
        the underlying work (running + queued); the rest coalesce."""
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp))

            work_calls: list[str] = []
            work_lock = threading.Lock()
            first_entered = threading.Event()
            release = threading.Event()
            original_do_work = engine._do_sync_work  # type: ignore[attr-defined]

            def fake_work(*, project_id: str):
                with work_lock:
                    work_calls.append(project_id)
                    is_first = len(work_calls) == 1
                if is_first:
                    first_entered.set()
                    release.wait(timeout=5.0)
                return original_do_work(project_id=project_id)

            engine._do_sync_work = fake_work  # type: ignore[assignment]

            threads = [
                threading.Thread(
                    target=lambda: engine.sync(project_id="p1"),
                    daemon=True,
                )
                for _ in range(5)
            ]
            for t in threads:
                t.start()

            self.assertTrue(first_entered.wait(timeout=2.0))
            # Give the other 4 callers time to arrive at the queue check.
            time.sleep(0.2)

            release.set()
            for t in threads:
                t.join(timeout=5.0)
                self.assertFalse(t.is_alive())

            # The queue bound: max one running + one queued = 2 work executions.
            self.assertEqual(len(work_calls), 2)

    def test_blocking_default_waits_for_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp))
            gate = self._pause_applier(engine)

            entered = threading.Event()
            results: list = []
            errors: list[BaseException] = []

            def first():
                try:
                    entered.set()
                    engine.sync(project_id="p1")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=first, daemon=True)
            t1.start()
            self.assertTrue(entered.wait(timeout=2.0))
            time.sleep(0.05)

            def second():
                try:
                    r = engine.sync(project_id="p1")
                    results.append(r)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            t2 = threading.Thread(target=second, daemon=True)
            t2.start()

            # Confirm second is genuinely blocked while first holds the lock.
            time.sleep(0.1)
            self.assertTrue(t2.is_alive())
            self.assertEqual(results, [])

            gate.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].skipped_busy)

    def test_skip_if_busy_does_not_skip_when_lock_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp))
            result = engine.sync(
                project_id="p1",
                skip_if_busy=True,
            )
            self.assertFalse(result.skipped_busy)

    def test_skip_if_busy_skips_when_repo_lock_is_busy_for_other_project(self) -> None:
        """The repo-wide lock prevents cross-project sync passes from racing."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            baseline = BaselineStore(db_path=tmp_path / "sync.sqlite")

            gate = threading.Event()
            a_volume_requested = threading.Event()

            def volume_provider(name: str) -> _StubVolume:
                # Pause only when proj_a is being processed; proj_b sails by.
                if name.endswith("-proj_a"):
                    a_volume_requested.set()
                    gate.wait(timeout=5.0)
                return _StubVolume()

            engine = SyncEngine(
                repo_root=repo,
                baseline=baseline,
                volume_provider=volume_provider,
            )

            def first() -> None:
                engine.sync(project_id="proj_a")

            t = threading.Thread(target=first, daemon=True)
            t.start()
            self.assertTrue(a_volume_requested.wait(timeout=2.0))

            # proj_b takes its own project queue slot, but the repo-wide sync
            # lock is held by proj_a, so poller-style callers skip.
            start = time.monotonic()
            result_b = engine.sync(
                project_id="proj_b",
                skip_if_busy=True,
            )
            elapsed = time.monotonic() - start
            self.assertTrue(result_b.skipped_busy)
            self.assertLess(elapsed, 0.5)

            gate.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())


class InterProcessSyncLockTest(unittest.TestCase):
    def test_nonblocking_acquire_fails_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".research_plugin" / "modal" / "sync.lock"
            lock = InterProcessSyncLock(lock_path=lock_path)
            with lock.acquire(blocking=True) as acquired:
                self.assertTrue(acquired)
                self.assertEqual(_child_lock_attempt(lock_path=lock_path), "busy")

            self.assertEqual(_child_lock_attempt(lock_path=lock_path), "acquired")


class SyncPollerGateTest(unittest.TestCase):
    def test_tick_skips_project_when_gate_blocks(self) -> None:
        baseline = _PollerBaseline(["proj_1"])
        engine = _RecordingPollEngine()
        events: list[tuple[str, dict]] = []
        poller = SyncPoller(
            engine=engine,  # type: ignore[arg-type]
            baseline=baseline,  # type: ignore[arg-type]
            should_sync_project=lambda project_id: project_id != "proj_1",
            activity=lambda event_type, payload: events.append((event_type, payload)),
        )

        poller._tick()

        self.assertEqual(engine.sync_calls, [])
        self.assertEqual(baseline.polled_projects, ["proj_1"])
        self.assertEqual(events, [("modal.sync.skipped_project_gate", {"project_id": "proj_1"})])


class _PollerBaseline:
    def __init__(self, project_ids: list[str]) -> None:
        self.project_ids = project_ids
        self.polled_projects: list[str] = []

    def known_projects(self) -> list[str]:
        return list(self.project_ids)

    def mark_polled(self, *, project_id: str, when: str) -> None:  # noqa: ARG002
        self.polled_projects.append(project_id)


class _RecordingPollEngine:
    def __init__(self) -> None:
        self.sync_calls: list[tuple[str, bool]] = []

    def sync(self, *, project_id: str, skip_if_busy: bool = False) -> SyncResult:
        self.sync_calls.append((project_id, skip_if_busy))
        return SyncResult(project_id=project_id)


def _child_lock_attempt(*, lock_path: Path) -> str:
    mcp_path = Path(__file__).resolve().parents[1] / "mcp_server"
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(mcp_path)
        if not existing_pythonpath
        else f"{mcp_path}{os.pathsep}{existing_pythonpath}"
    )
    script = """
import sys
from pathlib import Path
from backend.execution.backends.modal.sync.lock import InterProcessSyncLock

lock = InterProcessSyncLock(lock_path=Path(sys.argv[1]))
with lock.acquire(blocking=False) as acquired:
    print("acquired" if acquired else "busy")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, str(lock_path)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout.strip()


if __name__ == "__main__":
    unittest.main()
