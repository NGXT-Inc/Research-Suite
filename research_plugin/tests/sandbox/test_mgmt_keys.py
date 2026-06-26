"""Management keypair + dual-key bootstrap (cloud plan Phase 5, decision 4).

Both keys are generated on one machine in local mode, but the separation is
real and tested here: the user key is data-plane property (rsync, the sbx
dispatcher), the management key is control-plane property (transcript reads,
metrics sampling), the bootstraps authorize both, and private
key material never reaches requests, rows, or rendered bootstrap content.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.execution.backends.lambda_labs.sandbox_backend import (
    build_user_data,
)
from backend.execution.vm_bootstrap import MGMT_EXEC_SCRIPT, MGMT_SSH_USER
from backend.execution.backends.modal.sandbox_backend import BOOT_SCRIPT
from backend.state.managed_mgmt_keys import MountedMgmtKeyStore
from backend.state.mgmt_keys import LocalMgmtKeyStore
from backend.utils import ValidationError


class LocalMgmtKeyStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "mgmt_keys"
        self.store = LocalMgmtKeyStore(root=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ensure_mints_an_ed25519_keypair_under_the_store_root(self) -> None:
        public_key = self.store.ensure(sandbox_uid="exp_1")
        self.assertTrue(public_key.startswith("ssh-ed25519 "))
        key_path = self.store.key_path(sandbox_uid="exp_1")
        self.assertEqual(key_path, self.root / "exp_1" / "key")
        self.assertTrue(key_path.exists())
        self.assertTrue(key_path.with_suffix(".pub").exists())
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)

    def test_ensure_is_idempotent(self) -> None:
        first = self.store.ensure(sandbox_uid="exp_1")
        second = self.store.ensure(sandbox_uid="exp_1")
        self.assertEqual(first, second)

    def test_remove_drops_the_keypair(self) -> None:
        self.store.ensure(sandbox_uid="exp_1")
        self.store.remove(sandbox_uid="exp_1")
        key_path = self.store.key_path(sandbox_uid="exp_1")
        self.assertFalse(key_path.exists())
        self.assertFalse(key_path.with_suffix(".pub").exists())
        # Idempotent on an absent pair.
        self.store.remove(sandbox_uid="exp_1")


class MountedMgmtKeyStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key_path = self.root / "mounted_key"
        self.key_path.write_text("PRIVATE KEY\n", encoding="utf-8")
        self.key_path.chmod(0o600)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ensure_uses_adjacent_public_key_without_mutating_secret(self) -> None:
        public_path = Path(f"{self.key_path}.pub")
        public_path.write_text("ssh-ed25519 AAAAmounted\n", encoding="utf-8")
        store = MountedMgmtKeyStore(private_key_path=self.key_path)

        self.assertEqual(store.ensure(sandbox_uid="exp_1"), "ssh-ed25519 AAAAmounted")
        self.assertEqual(store.key_path(sandbox_uid="exp_1"), self.key_path)
        store.remove(sandbox_uid="exp_1")
        self.assertTrue(self.key_path.exists())
        self.assertTrue(public_path.exists())

    def test_public_key_env_override_does_not_need_adjacent_pub_file(self) -> None:
        store = MountedMgmtKeyStore(
            private_key_path=self.key_path,
            public_key="ssh-ed25519 AAAAconfigured",
        )

        self.assertEqual(
            store.ensure(sandbox_uid="exp_2"), "ssh-ed25519 AAAAconfigured"
        )

    def test_missing_public_key_fails_fast(self) -> None:
        with self.assertRaises(ValidationError):
            MountedMgmtKeyStore(private_key_path=self.key_path)

    def test_missing_private_key_fails_fast(self) -> None:
        with self.assertRaises(ValidationError):
            MountedMgmtKeyStore(
                private_key_path=self.root / "absent",
                public_key="ssh-ed25519 AAAAconfigured",
            )

    def test_open_permissions_fail_fast(self) -> None:
        self.key_path.chmod(0o644)

        with self.assertRaises(ValidationError):
            MountedMgmtKeyStore(
                private_key_path=self.key_path,
                public_key="ssh-ed25519 AAAAconfigured",
            )

    def test_rotated_private_key_fails_fast(self) -> None:
        store = MountedMgmtKeyStore(
            private_key_path=self.key_path,
            public_key="ssh-ed25519 AAAAconfigured",
        )
        self.key_path.write_text("DIFFERENT PRIVATE KEY\n", encoding="utf-8")

        with self.assertRaises(ValidationError):
            store.key_path(sandbox_uid="exp_1")


class DualKeyProvisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.project_id = self.call("project.create", name="Mgmt Keys")["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _experiment(self) -> str:
        exp_id = self.call(
            "experiment.create", name="exp-1", project_id=self.project_id, intent="x"
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?", (exp_id,)
            )
        return exp_id

    def test_request_carries_both_public_keys_and_bootstrap_authorizes_both(self) -> None:
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        request = self.backend.acquired[-1]
        self.assertTrue(request.public_key.startswith("ssh-ed25519 "))
        self.assertTrue(request.management_public_key.startswith("ssh-ed25519 "))
        # Separation is real: two distinct keypairs in two distinct homes.
        self.assertNotEqual(request.public_key, request.management_public_key)
        mgmt_keys = self.app.sandboxes.mgmt_keys
        key_path = mgmt_keys.key_path(sandbox_uid=created["sandbox_uid"])
        self.assertEqual(
            request.management_public_key,
            key_path.with_suffix(".pub").read_text().strip(),
        )
        user_key = self.repo / ".research_plugin" / "sandboxes" / "keys" / exp_id
        self.assertNotEqual(key_path.resolve(), user_key.resolve())
        # The captured bootstrap authorizes exactly the two keys.
        boot = self.backend.bootstraps[created["sandbox_id"]]
        self.assertEqual(
            boot["authorized_keys"],
            [request.public_key, request.management_public_key],
        )

    def test_row_records_a_mgmt_key_ref_but_never_key_material(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["mgmt_key_ref"], row["sandbox_uid"])
        private_key = self.app.sandboxes.mgmt_keys.key_path(sandbox_uid=row["sandbox_uid"]).read_text()
        for value in row.values():
            self.assertNotIn(private_key, str(value))

    def test_release_drops_the_management_keypair_with_the_sandbox(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        key_path = self.app.sandboxes.mgmt_keys.key_path(sandbox_uid=row["sandbox_uid"])
        self.assertTrue(key_path.exists())
        self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        # Per-sandbox keys: the keypair dies with the sandbox; the next
        # provision mints a fresh one.
        self.assertFalse(key_path.exists())


class BootstrapContentTest(unittest.TestCase):
    USER_KEY = "ssh-ed25519 AAAAuser research-plugin-exp1"
    MGMT_KEY = "ssh-ed25519 AAAAmgmt research-plugin-mgmt-exp1"

    def _user_data(self, *, management_public_key: str = "") -> str:
        return build_user_data(
            public_key=self.USER_KEY,
            experiment_id="exp1",
            workdir="/workspace/exp1",
            sessions_dir="/workspace/.research_plugin_sessions/exp1",
            sandbox_data_dir="/workspace/data",
            management_public_key=management_public_key,
        )

    def test_modal_boot_script_authorizes_both_keys(self) -> None:
        self.assertIn("RP_AUTHORIZED_KEY", BOOT_SCRIPT)
        self.assertIn("RP_MANAGEMENT_KEY", BOOT_SCRIPT)
        # Both append into the same authorized_keys; the management append
        # must come from its own env value, never duplicate the user one.
        self.assertLess(
            BOOT_SCRIPT.index("RP_AUTHORIZED_KEY"),
            BOOT_SCRIPT.index("RP_MANAGEMENT_KEY"),
        )

    def test_lambda_user_data_provisions_the_management_principal(self) -> None:
        user_data = self._user_data(management_public_key=self.MGMT_KEY)
        self.assertIn(f"useradd --create-home --shell /bin/bash {MGMT_SSH_USER}", user_data)
        self.assertIn(f"/home/{MGMT_SSH_USER}/.ssh/authorized_keys", user_data)
        self.assertIn(f"{MGMT_SSH_USER} ALL=(ALL) NOPASSWD:ALL", user_data)
        # The Match exemption from the global rec.sh ForceCommand goes at the
        # END of the main sshd_config — a Match opened in sshd_config.d would
        # swallow the distro directives after the Include point.
        self.assertIn(f"Match User {MGMT_SSH_USER}", user_data)
        self.assertIn("ForceCommand /opt/rp/mgmt_exec.sh", user_data)
        self.assertIn("cat >> /etc/ssh/sshd_config <<'RP_SSHD_MATCH'", user_data)
        self.assertIn("ForceCommand /opt/rp/rec.sh", user_data)
        # The Match block lands before the sshd restart, so the exemption is
        # live the moment the daemon reads anything.
        self.assertLess(
            user_data.index("RP_SSHD_MATCH"), user_data.index("systemctl restart ssh")
        )

    def test_lambda_user_data_without_mgmt_key_keeps_legacy_shape(self) -> None:
        user_data = self._user_data()
        self.assertNotIn(MGMT_SSH_USER, user_data)
        self.assertNotIn("Match User", user_data)
        self.assertIn("ForceCommand /opt/rp/rec.sh", user_data)

    def test_user_data_never_embeds_private_key_material(self) -> None:
        user_data = self._user_data(management_public_key=self.MGMT_KEY)
        self.assertNotIn("PRIVATE KEY", user_data)

    def test_mgmt_exec_script_is_a_raw_pass_through(self) -> None:
        # The management ForceCommand must exec the client command untouched —
        # no tee into the transcript, no markers.
        self.assertIn('exec bash -lc "${SSH_ORIGINAL_COMMAND:-bash -l}"', MGMT_EXEC_SCRIPT)
        self.assertNotIn("tee", MGMT_EXEC_SCRIPT)
        self.assertNotIn("$LOG", MGMT_EXEC_SCRIPT)


if __name__ == "__main__":
    unittest.main()
