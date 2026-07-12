"""Resolution and exclusion of the per-project checkout state dir.

Projects linked before v0.0013 keep `.research_plugin/`; fresh checkouts get
`.merv/`. Both names are excluded from repo-relative path surfaces
unconditionally.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research_plugin_shared.project_dirs import (
    LEGACY_PROJECT_STATE_DIR,
    PROJECT_STATE_DIR,
    PROJECT_STATE_DIR_NAMES,
    ensure_project_state_dir,
    resolve_project_state_dir,
)

from backend.dataplane.repo_paths import repo_relative_path
from backend.state.activity import ActivityLogger
from backend.utils import ValidationError
from backend.workspace import LocalWorkspace


class ResolveProjectStateDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repo_with_existing_legacy_dir_resolves_to_it(self) -> None:
        (self.repo / LEGACY_PROJECT_STATE_DIR).mkdir()
        self.assertEqual(
            resolve_project_state_dir(self.repo),
            self.repo / LEGACY_PROJECT_STATE_DIR,
        )

    def test_fresh_repo_resolves_to_merv(self) -> None:
        self.assertEqual(
            resolve_project_state_dir(self.repo), self.repo / PROJECT_STATE_DIR
        )

    def test_legacy_wins_when_both_dirs_exist(self) -> None:
        (self.repo / LEGACY_PROJECT_STATE_DIR).mkdir()
        (self.repo / PROJECT_STATE_DIR).mkdir()
        self.assertEqual(
            resolve_project_state_dir(self.repo),
            self.repo / LEGACY_PROJECT_STATE_DIR,
        )

    def test_stray_legacy_file_does_not_hijack_resolution(self) -> None:
        (self.repo / LEGACY_PROJECT_STATE_DIR).write_text("not a dir\n")
        self.assertEqual(
            resolve_project_state_dir(self.repo), self.repo / PROJECT_STATE_DIR
        )

    def test_ensure_creates_a_self_ignoring_state_dir(self) -> None:
        state = ensure_project_state_dir(self.repo)
        self.assertEqual(state, self.repo / PROJECT_STATE_DIR)
        self.assertTrue(state.is_dir())
        self.assertEqual((state / ".gitignore").read_text(), "*\n")

    def test_ensure_respects_legacy_dir_and_existing_gitignore(self) -> None:
        legacy = self.repo / LEGACY_PROJECT_STATE_DIR
        legacy.mkdir()
        (legacy / ".gitignore").write_text("keys/\n")
        state = ensure_project_state_dir(self.repo)
        self.assertEqual(state, legacy)
        self.assertEqual((legacy / ".gitignore").read_text(), "keys/\n")

    def test_workspace_and_activity_log_route_through_the_resolver(self) -> None:
        for state_dir, prepare in (
            (PROJECT_STATE_DIR, lambda: None),
            (
                LEGACY_PROJECT_STATE_DIR,
                lambda: (self.repo / LEGACY_PROJECT_STATE_DIR).mkdir(),
            ),
        ):
            with self.subTest(state_dir=state_dir), tempfile.TemporaryDirectory() as tmp:
                self.repo = Path(tmp)
                prepare()
                workspace = LocalWorkspace(repo_root=self.repo)
                resolved = self.repo.resolve() / state_dir
                self.assertEqual(workspace.research_dir, resolved)
                self.assertEqual(
                    workspace.sessions_dir(experiment_id="exp_1", sandbox_id="sb_1"),
                    resolved / "sessions" / "exp_1" / "sb_1",
                )
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if k != "RESEARCH_PLUGIN_ACTIVITY_LOG_PATH"
                }
                with mock.patch.dict(os.environ, env, clear=True):
                    logger = ActivityLogger(
                        repo_root=self.repo, enabled=True, mirror_stderr=False
                    )
                self.assertEqual(logger.log_path, self.repo / state_dir / "activity.jsonl")
                logger.emit(event_type="test.event", payload={})
                self.assertTrue((self.repo / state_dir / "activity.jsonl").is_file())


class ProjectStateDirExclusionTest(unittest.TestCase):
    def test_repo_relative_path_rejects_both_names(self) -> None:
        for state_dir in PROJECT_STATE_DIR_NAMES:
            with self.subTest(state_dir=state_dir):
                with self.assertRaises(ValidationError):
                    repo_relative_path(path=f"{state_dir}/anything.txt")

    def test_repo_relative_path_allows_lookalike_names(self) -> None:
        # Only the exact first path part is excluded; superstring dirnames
        # (the in-VM `.research_plugin_sessions` convention) pass through.
        self.assertEqual(
            repo_relative_path(path=".research_plugin_sessions/x.txt"),
            ".research_plugin_sessions/x.txt",
        )


if __name__ == "__main__":
    unittest.main()
