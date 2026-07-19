"""Resolution of the machine-level (home) client-state dir.

Machines configured before v0.0014 keep ``~/.research_plugin/``; fresh
machines get ``~/.merv/``. All home-scoped client paths (client.json,
project_links.sqlite, daemon_secret) follow one resolution.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from merv.shared.machine_dirs import (
    LEGACY_MACHINE_STATE_DIR,
    MACHINE_STATE_DIR,
    resolve_machine_state_dir,
)
from merv.shared.client_config import (
    default_client_config_path,
    default_daemon_secret_path,
    resolve_client_config_path,
)
from merv.proxy.project_links import default_project_links_path


class ResolveMachineStateDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_machine_resolves_to_merv(self) -> None:
        self.assertEqual(
            resolve_machine_state_dir(self.home), self.home / MACHINE_STATE_DIR
        )

    def test_machine_with_legacy_dir_keeps_it(self) -> None:
        (self.home / LEGACY_MACHINE_STATE_DIR).mkdir()
        self.assertEqual(
            resolve_machine_state_dir(self.home),
            self.home / LEGACY_MACHINE_STATE_DIR,
        )

    def test_legacy_wins_when_both_dirs_exist(self) -> None:
        (self.home / LEGACY_MACHINE_STATE_DIR).mkdir()
        (self.home / MACHINE_STATE_DIR).mkdir()
        self.assertEqual(
            resolve_machine_state_dir(self.home),
            self.home / LEGACY_MACHINE_STATE_DIR,
        )

    def test_stray_legacy_file_does_not_hijack_resolution(self) -> None:
        (self.home / LEGACY_MACHINE_STATE_DIR).write_text("not a dir\n")
        self.assertEqual(
            resolve_machine_state_dir(self.home), self.home / MACHINE_STATE_DIR
        )


class MachineStatePathDefaultsTest(unittest.TestCase):
    """client.json / daemon_secret / project_links follow the one resolution."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_fresh_machine_defaults_land_under_merv(self) -> None:
        merv = self.home / MACHINE_STATE_DIR
        self.assertEqual(default_client_config_path(), merv / "client.json")
        self.assertEqual(default_daemon_secret_path(), merv / "daemon_secret")
        self.assertEqual(
            default_project_links_path(), merv / "project_links.sqlite"
        )
        self.assertEqual(resolve_client_config_path(env={}), merv / "client.json")

    def test_legacy_machine_defaults_stay_put(self) -> None:
        legacy = self.home / LEGACY_MACHINE_STATE_DIR
        legacy.mkdir()
        self.assertEqual(default_client_config_path(), legacy / "client.json")
        self.assertEqual(default_daemon_secret_path(), legacy / "daemon_secret")
        self.assertEqual(
            default_project_links_path(), legacy / "project_links.sqlite"
        )
        self.assertEqual(resolve_client_config_path(env={}), legacy / "client.json")

    def test_explicit_env_override_bypasses_detection(self) -> None:
        (self.home / LEGACY_MACHINE_STATE_DIR).mkdir()
        explicit = self.home / "elsewhere" / "client.json"
        for spelling in ("MERV_CLIENT_CONFIG", "RESEARCH_PLUGIN_CLIENT_CONFIG"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    resolve_client_config_path(env={spelling: str(explicit)}),
                    explicit,
                )


if __name__ == "__main__":
    unittest.main()
