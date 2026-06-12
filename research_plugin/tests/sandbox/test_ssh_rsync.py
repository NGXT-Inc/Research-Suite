from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.execution import ssh_rsync
from backend.execution.ssh_rsync import RsyncBinary, SshRsyncSyncer


class SshRsyncSyncerTest(unittest.TestCase):
    def test_builds_general_and_artifact_passes(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 0, stdout="metrics.json\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="model.safetensors\n", stderr="")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = root / "id_ed25519"
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)

            result = syncer.sync(
                ssh_host="127.0.0.1",
                ssh_port=2222,
                ssh_user="root",
                key_path=key,
                remote_sync_dir="/workspace/synced",
                local_sync_dir=root / "local",
            )

        self.assertEqual(result.pulled, 2)
        self.assertEqual(result.command_count, 2)
        self.assertEqual(len(calls), 2)
        self.assertIn("--max-size=100m", calls[0])
        self.assertIn("--max-size=5g", calls[1])
        self.assertIn("--exclude", calls[0])
        self.assertIn("artifacts_to_keep/", calls[0])
        self.assertIn("root@127.0.0.1:/workspace/synced/", calls[0])
        self.assertIn("root@127.0.0.1:/workspace/synced/artifacts_to_keep/", calls[1])
        self.assertNotIn("--ignore-missing-args", calls[1])

    def test_missing_artifact_dir_is_tolerated(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 0, stdout="metrics.json\n", stderr="")
            return subprocess.CompletedProcess(command, 23, stdout="", stderr="missing")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = root / "id_ed25519"
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)

            result = syncer.sync(
                ssh_host="127.0.0.1",
                ssh_port=2222,
                ssh_user="root",
                key_path=key,
                remote_sync_dir="/workspace/synced",
                local_sync_dir=root / "local",
            )

        self.assertEqual(result.pulled, 1)
        self.assertEqual(result.command_count, 2)

    def test_sessions_pass_pulls_telemetry_into_daemon_dir(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if len(calls) == 3:
                return subprocess.CompletedProcess(command, 0, stdout="mlflow.db\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = root / "id_ed25519"
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)

            result = syncer.sync(
                ssh_host="127.0.0.1",
                ssh_port=2222,
                ssh_user="root",
                key_path=key,
                remote_sync_dir="/workspace/exp_1",
                local_sync_dir=root / "local",
                remote_sessions_dir="/workspace/.research_plugin_sessions/exp_1",
                local_sessions_dir=root / "sessions" / "exp_1" / "sb-1",
            )

        self.assertEqual(result.command_count, 3)
        self.assertIn(
            "root@127.0.0.1:/workspace/.research_plugin_sessions/exp_1/", calls[2]
        )
        self.assertTrue(str(calls[2][-1]).endswith("sessions/exp_1/sb-1/"))

    def test_missing_sessions_dir_is_tolerated(self) -> None:
        # A legacy sandbox keeps its telemetry inside the synced folder, so the
        # dedicated sessions path simply does not exist remotely (rsync 23).
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            if "/.research_plugin_sessions/" in command[-2]:
                return subprocess.CompletedProcess(command, 23, stdout="", stderr="missing")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = root / "id_ed25519"
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)

            result = syncer.sync(
                ssh_host="127.0.0.1",
                ssh_port=2222,
                ssh_user="root",
                key_path=key,
                remote_sync_dir="/workspace/synced",
                local_sync_dir=root / "local",
                remote_sessions_dir="/workspace/.research_plugin_sessions/exp_1",
                local_sessions_dir=root / "sessions" / "exp_1" / "sb-1",
            )

        self.assertEqual(result.command_count, 3)

    def test_push_initial_builds_local_to_remote_passes(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 0, stdout="seed.py\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="model.bin\n", stderr="")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = root / "id_ed25519"
            local = root / "local"
            (local / "artifacts_to_keep").mkdir(parents=True)
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)

            result = syncer.push_initial(
                ssh_host="127.0.0.1",
                ssh_port=2222,
                ssh_user="root",
                key_path=key,
                remote_sync_dir="/workspace/synced",
                local_sync_dir=local,
            )

        self.assertEqual(result.direction, "push")
        self.assertEqual(result.pulled, 2)
        self.assertEqual(result.as_dict()["direction"], "push")
        self.assertEqual(calls[0][-2], str(local) + "/")
        self.assertEqual(calls[0][-1], "root@127.0.0.1:/workspace/synced/")
        self.assertEqual(calls[1][-2], str(local / "artifacts_to_keep") + "/")
        self.assertEqual(calls[1][-1], "root@127.0.0.1:/workspace/synced/artifacts_to_keep/")
        # The sandbox-authored sessions dir is protected from the push's
        # --delete: the sandbox creates dashboard pids/logs there at boot,
        # before the first push, and the push must never try to remove them.
        main_push = calls[0]
        self.assertIn("--delete", main_push)
        exclude_values = [
            main_push[i + 1] for i, arg in enumerate(main_push) if arg == "--exclude"
        ]
        self.assertIn(".research_plugin_sessions/", exclude_values)

    def test_push_initial_missing_artifact_dir_is_tolerated(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 0, stdout="seed.py\n", stderr="")
            return subprocess.CompletedProcess(command, 23, stdout="", stderr="missing")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = root / "id_ed25519"
            local = root / "local"
            local.mkdir()
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)

            result = syncer.push_initial(
                ssh_host="127.0.0.1",
                ssh_port=2222,
                ssh_user="root",
                key_path=key,
                remote_sync_dir="/workspace/synced",
                local_sync_dir=local,
            )

        self.assertEqual(result.direction, "push")
        self.assertEqual(result.pulled, 1)
        self.assertEqual(result.command_count, 2)

    def test_general_pass_failure_raises(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 23, stdout="", stderr="source missing")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = root / "id_ed25519"
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)

            with self.assertRaisesRegex(RuntimeError, "rsync failed"):
                syncer.sync(
                    ssh_host="127.0.0.1",
                    ssh_port=2222,
                    ssh_user="root",
                    key_path=key,
                    remote_sync_dir="/workspace/synced",
                    local_sync_dir=root / "local",
                )


class RsyncBinaryResolutionTest(unittest.TestCase):
    def test_too_old_rsync_raises_actionable_error(self) -> None:
        old = RsyncBinary(path="/usr/bin/rsync", version=(2, 6, 9))
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "id_ed25519"
            key.write_text("test-key")
            # No custom runner -> the version gate is active.
            syncer = SshRsyncSyncer()
            with mock.patch.object(ssh_rsync, "resolve_rsync", return_value=old):
                with self.assertRaisesRegex(RuntimeError, "too old"):
                    syncer.sync(
                        ssh_host="127.0.0.1",
                        ssh_port=2222,
                        ssh_user="root",
                        key_path=key,
                        remote_sync_dir="/workspace/synced",
                        local_sync_dir=Path(td) / "local",
                    )

    def test_resolved_binary_is_first_command_token(self) -> None:
        modern = RsyncBinary(path="/opt/homebrew/bin/rsync", version=(3, 4, 3))
        calls: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="metrics.json\n", stderr="")

        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "id_ed25519"
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)
            with mock.patch.object(ssh_rsync, "resolve_rsync", return_value=modern):
                syncer.sync(
                    ssh_host="127.0.0.1",
                    ssh_port=2222,
                    ssh_user="root",
                    key_path=key,
                    remote_sync_dir="/workspace/synced",
                    local_sync_dir=Path(td) / "local",
                )

        self.assertEqual(calls[0][0], "/opt/homebrew/bin/rsync")

    def test_custom_runner_skips_version_gate(self) -> None:
        # A too-old binary must NOT block when a runner is injected (test seam).
        old = RsyncBinary(path="/usr/bin/rsync", version=(2, 6, 9))

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "id_ed25519"
            key.write_text("test-key")
            syncer = SshRsyncSyncer(runner=runner)
            with mock.patch.object(ssh_rsync, "resolve_rsync", return_value=old):
                result = syncer.sync(
                    ssh_host="127.0.0.1",
                    ssh_port=2222,
                    ssh_user="root",
                    key_path=key,
                    remote_sync_dir="/workspace/synced",
                    local_sync_dir=Path(td) / "local",
                )
        self.assertEqual(result.command_count, 2)


if __name__ == "__main__":
    unittest.main()
