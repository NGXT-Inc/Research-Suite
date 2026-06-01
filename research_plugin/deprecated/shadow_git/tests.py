"""Self-contained tests for the shadow-git subpackage.

These tests do not import anything from the surrounding research-plugin app,
which keeps shadow git verifiable as an isolated unit.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from . import (
    ShadowGitPathError,
    ShadowGitStore,
    ShadowGitUnavailableError,
    SnapshotUnavailableError,
)
from . import _policy as policy


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        # Best-effort recursive cleanup; tests are local-only.
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel: str, body: str | bytes) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, str):
            path.write_text(body)
        else:
            path.write_bytes(body)
        return path

    def _store(self, *, enabled: bool = True, max_bytes: int | None = None) -> ShadowGitStore:
        return ShadowGitStore(repo_root=self.tmp, enabled=enabled, max_snapshot_bytes=max_bytes)


class DisabledStoreTests(_Base):
    def test_disabled_snapshot_skips_git_and_returns_metadata_only(self) -> None:
        target = self._write("plan.md", "# hi\n")
        store = self._store(enabled=False)
        result = store.snapshot_file(
            project_id="p1", rel_path="plan.md", file_path=target,
            observed_at="t", created_by="codex",
        )
        self.assertEqual(result["snapshot_status"], "metadata_only")
        self.assertIsNone(result["git_commit"])
        self.assertFalse((self.tmp / ".research_plugin" / "resource_store.git").exists())

    def test_disabled_reads_raise_snapshot_unavailable(self) -> None:
        store = self._store(enabled=False)
        with self.assertRaises(SnapshotUnavailableError):
            store.version_text(git_commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", git_path="x")
        with self.assertRaises(SnapshotUnavailableError):
            store.diff(
                from_commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                to_commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                git_path="x",
            )


class EnabledStoreTests(_Base):
    def test_round_trip_stored_text(self) -> None:
        target = self._write("plan.md", "# Attempt 1\nhello\n")
        store = self._store(enabled=True)
        snap = store.snapshot_file(
            project_id="p1", rel_path="plan.md", file_path=target,
            observed_at="t1", created_by="codex",
        )
        self.assertEqual(snap["snapshot_status"], "stored")
        text = store.version_text(git_commit=snap["git_commit"], git_path=snap["git_path"])
        self.assertEqual(text, "# Attempt 1\nhello\n")

    def test_identical_content_reuses_previous_commit(self) -> None:
        target = self._write("plan.md", "same\n")
        store = self._store(enabled=True)
        first = store.snapshot_file(
            project_id="p1", rel_path="plan.md", file_path=target,
            observed_at="t1", created_by="codex",
        )
        second = store.snapshot_file(
            project_id="p1", rel_path="plan.md", file_path=target,
            observed_at="t2", created_by="codex",
        )
        self.assertEqual(first["git_commit"], second["git_commit"])

    def test_diff_between_two_versions(self) -> None:
        target = self._write("plan.md", "alpha\n")
        store = self._store(enabled=True)
        a = store.snapshot_file(
            project_id="p1", rel_path="plan.md", file_path=target,
            observed_at="t1", created_by="codex",
        )
        target.write_text("beta\n")
        b = store.snapshot_file(
            project_id="p1", rel_path="plan.md", file_path=target,
            observed_at="t2", created_by="codex",
        )
        out = store.diff(from_commit=a["git_commit"], to_commit=b["git_commit"], git_path=a["git_path"])
        self.assertIn("-alpha", out)
        self.assertIn("+beta", out)

    def test_file_to_directory_transition(self) -> None:
        store = self._store(enabled=True)
        leaf = self._write("foo", "leaf\n")
        first = store.snapshot_file(
            project_id="p1", rel_path="foo", file_path=leaf,
            observed_at="t1", created_by="codex",
        )
        self.assertEqual(first["snapshot_status"], "stored")
        leaf.unlink()
        nested = self._write("foo/bar.md", "nested\n")
        second = store.snapshot_file(
            project_id="p1", rel_path="foo/bar.md", file_path=nested,
            observed_at="t2", created_by="codex",
        )
        self.assertEqual(second["snapshot_status"], "stored")
        self.assertEqual(
            store.version_text(git_commit=second["git_commit"], git_path=second["git_path"]),
            "nested\n",
        )

    def test_recover_from_dirty_index_without_head(self) -> None:
        # Simulate a crash on the very first snapshot: index has a staged file
        # but no commit landed yet. Next snapshot must still succeed.
        store = self._store(enabled=True)
        store._git.ensure_initialised()  # noqa: SLF001 - exercising recovery
        (store.git_root / "stray.txt").write_text("stale\n")
        store._git.call(("add", "--", "stray.txt"))  # noqa: SLF001

        target = self._write("plan.md", "after-crash\n")
        snap = store.snapshot_file(
            project_id="p1", rel_path="plan.md", file_path=target,
            observed_at="t", created_by="codex",
        )
        self.assertEqual(snap["snapshot_status"], "stored")
        self.assertFalse((store.git_root / "stray.txt").exists())
        self.assertEqual(
            store.version_text(git_commit=snap["git_commit"], git_path=snap["git_path"]),
            "after-crash\n",
        )


class CapacityAndContentTests(_Base):
    def test_large_text_falls_back_to_metadata_only(self) -> None:
        target = self._write("big.txt", "a" * 1024)
        store = self._store(enabled=True, max_bytes=512)
        result = store.snapshot_file(
            project_id="p1", rel_path="big.txt", file_path=target,
            observed_at="t", created_by="codex",
        )
        self.assertEqual(result["snapshot_status"], "metadata_only")
        self.assertIsNone(result["git_commit"])

    def test_binary_falls_back_to_metadata_only(self) -> None:
        target = self._write("bin", b"\x00\x01\x02\x03")
        store = self._store(enabled=True)
        result = store.snapshot_file(
            project_id="p1", rel_path="bin", file_path=target,
            observed_at="t", created_by="codex",
        )
        self.assertEqual(result["snapshot_status"], "metadata_only")


class PathSafetyTests(_Base):
    def test_rejects_dotdot_segment(self) -> None:
        target = self._write("plan.md", "x")
        store = self._store(enabled=True)
        with self.assertRaises(ShadowGitPathError):
            store.snapshot_file(
                project_id="p1", rel_path="../plan.md", file_path=target,
                observed_at="t", created_by="codex",
            )

    def test_rejects_dot_git_segment(self) -> None:
        target = self._write("plan.md", "x")
        store = self._store(enabled=True)
        with self.assertRaises(ShadowGitPathError):
            store.snapshot_file(
                project_id="p1", rel_path="foo/.git/bar", file_path=target,
                observed_at="t", created_by="codex",
            )

    def test_rejects_absolute_rel_path(self) -> None:
        target = self._write("plan.md", "x")
        store = self._store(enabled=True)
        with self.assertRaises(ShadowGitPathError):
            store.snapshot_file(
                project_id="p1", rel_path="/abs/plan.md", file_path=target,
                observed_at="t", created_by="codex",
            )

    def test_invalid_commit_hash_rejected(self) -> None:
        store = self._store(enabled=True)
        with self.assertRaises(ShadowGitPathError):
            store.version_text(git_commit="..master", git_path="x")
        with self.assertRaises(ShadowGitPathError):
            store.diff(from_commit="zzz", to_commit="0" * 40, git_path="x")


class FailureSurfaceTests(_Base):
    def test_missing_git_binary_raises_clean_error(self) -> None:
        target = self._write("plan.md", "x")
        store = self._store(enabled=True)
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
            with self.assertRaises(ShadowGitUnavailableError):
                store.snapshot_file(
                    project_id="p1", rel_path="plan.md", file_path=target,
                    observed_at="t", created_by="codex",
                )


class ConcurrencyTests(_Base):
    def test_concurrent_snapshots_serialise_via_lock(self) -> None:
        store = self._store(enabled=True)

        commits: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(label: str) -> None:
            try:
                # Each worker owns its own file path so the test exercises real
                # concurrent git operations rather than a write race.
                rel = f"plans/plan-{label}.md"
                target = self._write(rel, f"body-{label}\n")
                snap = store.snapshot_file(
                    project_id="p1", rel_path=rel, file_path=target,
                    observed_at=f"t-{label}", created_by="codex",
                )
                with lock:
                    commits.append(snap["git_commit"])
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(commits), 4)
        self.assertEqual(len(set(commits)), len(commits))


class PolicyHelperTests(unittest.TestCase):
    def test_is_enabled_reads_env(self) -> None:
        with mock.patch.dict(os.environ, {"RESEARCH_PLUGIN_SHADOW_GIT_ENABLED": "1"}, clear=False):
            self.assertTrue(policy.is_enabled())
        with mock.patch.dict(os.environ, {"RESEARCH_PLUGIN_SHADOW_GIT_ENABLED": "0"}, clear=False):
            self.assertFalse(policy.is_enabled())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
