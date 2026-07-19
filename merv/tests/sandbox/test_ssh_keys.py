from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from merv.brain.sandbox.ssh_keys import ensure_ed25519_keypair
from merv.brain.kernel.utils import ValidationError


class SshKeysTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key_path = self.root / "keys" / "exp_1"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_existing_pair_returns_public_key_without_regenerating(self) -> None:
        self.key_path.parent.mkdir(parents=True)
        self.key_path.write_text("PRIVATE\n", encoding="utf-8")
        self.key_path.with_suffix(".pub").write_text(
            "ssh-ed25519 AAAAexisting\n", encoding="utf-8"
        )

        with patch("merv.brain.sandbox.ssh_keys.subprocess.run") as run:
            public_key = ensure_ed25519_keypair(
                key_path=self.key_path,
                comment="merv-exp_1",
                missing_action="provision sandbox SSH access",
                failure_subject="sandbox SSH key",
            )

        self.assertEqual(public_key, "ssh-ed25519 AAAAexisting")
        run.assert_not_called()

    def test_mints_keypair_and_sets_private_key_mode(self) -> None:
        def fake_run(cmd, **kwargs):
            self.assertEqual(kwargs, {"check": True, "capture_output": True, "text": True})
            self.assertEqual(cmd[-2:], ["-f", str(self.key_path)])
            self.assertIn("merv-exp_1", cmd)
            self.key_path.write_text("PRIVATE\n", encoding="utf-8")
            self.key_path.with_suffix(".pub").write_text(
                "ssh-ed25519 AAAAgenerated\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(cmd, 0)

        with patch("merv.brain.sandbox.ssh_keys.subprocess.run", side_effect=fake_run):
            public_key = ensure_ed25519_keypair(
                key_path=self.key_path,
                comment="merv-exp_1",
                missing_action="provision sandbox SSH access",
                failure_subject="sandbox SSH key",
            )

        self.assertEqual(public_key, "ssh-ed25519 AAAAgenerated")
        self.assertEqual(self.key_path.stat().st_mode & 0o777, 0o600)

    def test_missing_ssh_keygen_maps_to_validation_error(self) -> None:
        with patch(
            "merv.brain.sandbox.ssh_keys.subprocess.run", side_effect=FileNotFoundError()
        ):
            with self.assertRaisesRegex(
                ValidationError,
                "ssh-keygen is required to provision sandbox SSH access but was not found",
            ):
                ensure_ed25519_keypair(
                    key_path=self.key_path,
                    comment="merv-exp_1",
                    missing_action="provision sandbox SSH access",
                    failure_subject="sandbox SSH key",
                )

    def test_failed_ssh_keygen_maps_output_to_validation_error(self) -> None:
        failure = subprocess.CalledProcessError(
            1, ["ssh-keygen"], stderr="denied"
        )
        with patch("merv.brain.sandbox.ssh_keys.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(
                ValidationError,
                "failed to generate sandbox SSH key: denied",
            ):
                ensure_ed25519_keypair(
                    key_path=self.key_path,
                    comment="merv-exp_1",
                    missing_action="provision sandbox SSH access",
                    failure_subject="sandbox SSH key",
                )


if __name__ == "__main__":
    unittest.main()
