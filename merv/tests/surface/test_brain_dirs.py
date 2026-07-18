"""Local brain state-dir resolution: fresh de-nested, legacy-wins verbatim.

Fresh brains stage at ``~/.merv/brain`` with ``state.sqlite``/``blobs``/
``mgmt_keys`` directly inside; a machine with the pre-v0.0014 layout
(``~/.research_plugin/brain/.research_plugin/state.sqlite``) keeps every
legacy path forever. ``state.sqlite`` is the sentinel on both layers.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.composition import control_mode
from backend.composition.brain_dirs import (
    resolve_brain_state_root,
    resolve_local_brain_staging,
)
from backend.execution.backends.fake import FakeSandboxBackend


def _mounted_mgmt_key_env(root: Path) -> dict[str, str]:
    key_path = root / "managed_key"
    key_path.write_text("PRIVATE KEY\n", encoding="utf-8")
    key_path.chmod(0o600)
    return {
        "MERV_MGMT_KEY_PATH": str(key_path),
        "MERV_MGMT_PUBLIC_KEY": "ssh-ed25519 AAAAmanaged",
    }


class BrainStateRootTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_fresh_root_is_de_nested(self) -> None:
        self.assertEqual(resolve_brain_state_root(self.root), self.root)

    def test_legacy_nested_state_wins(self) -> None:
        legacy = self.root / ".research_plugin"
        legacy.mkdir()
        (legacy / "state.sqlite").write_bytes(b"")
        self.assertEqual(resolve_brain_state_root(self.root), legacy)

    def test_empty_legacy_dir_does_not_hijack_resolution(self) -> None:
        (self.root / ".research_plugin").mkdir()
        self.assertEqual(resolve_brain_state_root(self.root), self.root)


class LocalBrainStagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _materialize_legacy_brain(self) -> Path:
        legacy_state = self.home / ".research_plugin" / "brain" / ".research_plugin"
        legacy_state.mkdir(parents=True)
        (legacy_state / "state.sqlite").write_bytes(b"")
        return self.home / ".research_plugin" / "brain"

    def test_fresh_home_stages_under_merv(self) -> None:
        self.assertEqual(
            resolve_local_brain_staging(self.home), self.home / ".merv" / "brain"
        )

    def test_legacy_brain_state_pins_the_legacy_root(self) -> None:
        legacy_root = self._materialize_legacy_brain()
        self.assertEqual(resolve_local_brain_staging(self.home), legacy_root)
        # ...and the inner layout stays nested, so every path is verbatim.
        self.assertEqual(
            resolve_brain_state_root(legacy_root),
            legacy_root / ".research_plugin",
        )

    def test_legacy_client_dir_without_brain_state_stays_fresh(self) -> None:
        # A machine may keep legacy CLIENT state yet never have run a brain.
        (self.home / ".research_plugin").mkdir()
        self.assertEqual(
            resolve_local_brain_staging(self.home), self.home / ".merv" / "brain"
        )

    def test_local_brain_root_precedence(self) -> None:
        legacy_root = self._materialize_legacy_brain()
        with mock.patch("pathlib.Path.home", return_value=self.home):
            # Explicit state_dir wins outright.
            explicit = self.home / "explicit"
            self.assertEqual(
                control_mode._local_brain_root(state_dir=explicit, env=None),
                explicit.resolve(),
            )
            # The override env (either spelling) bypasses detection.
            override = self.home / "override"
            for spelling in ("MERV_LOCAL_STATE_DIR", "RESEARCH_PLUGIN_LOCAL_STATE_DIR"):
                with self.subTest(spelling=spelling):
                    self.assertEqual(
                        control_mode._local_brain_root(
                            state_dir=None, env={spelling: str(override)}
                        ),
                        override.resolve(),
                    )
            # No override: legacy state pins the legacy root.
            self.assertEqual(
                control_mode._local_brain_root(state_dir=None, env={}),
                legacy_root.resolve(),
            )


class BrainCompositionLayoutTest(unittest.TestCase):
    """build_control_app stages fresh roots de-nested, legacy roots verbatim."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _build(self):
        app, _queue = control_mode.build_control_app(
            repo_root=self.root,
            env=_mounted_mgmt_key_env(self.root),
            execution_backend=FakeSandboxBackend(),
        )
        self.addCleanup(app.shutdown)
        return app

    def test_fresh_root_produces_only_de_nested_paths(self) -> None:
        self._build()
        self.assertTrue((self.root / "state.sqlite").is_file())
        self.assertFalse((self.root / ".research_plugin").exists())

    def test_existing_legacy_layout_is_used_verbatim(self) -> None:
        legacy = self.root / ".research_plugin"
        legacy.mkdir()
        (legacy / "state.sqlite").write_bytes(b"")  # empty file = empty sqlite db
        # Legacy DIRECTORY state must never trigger the env deprecation
        # warning — only legacy env-var *input* does.
        with self.assertNoLogs("backend.env", level="WARNING"):
            self._build()
        # The legacy store was opened (migrations populate it) and no
        # de-nested twin ever appears: identical behavior to pre-v0.0014.
        self.assertGreater((legacy / "state.sqlite").stat().st_size, 0)
        self.assertFalse((self.root / "state.sqlite").exists())

    def test_local_mgmt_keys_follow_the_same_layout(self) -> None:
        legacy = self.root / ".research_plugin"
        legacy.mkdir()
        (legacy / "state.sqlite").write_bytes(b"")
        store = control_mode._build_mgmt_key_store(env=None, local_root=self.root)
        self.assertEqual(store.root, legacy / "mgmt_keys")
        fresh_root = self.root / "fresh"
        fresh_root.mkdir()
        store = control_mode._build_mgmt_key_store(env=None, local_root=fresh_root)
        self.assertEqual(store.root, fresh_root / "mgmt_keys")


if __name__ == "__main__":
    unittest.main()
