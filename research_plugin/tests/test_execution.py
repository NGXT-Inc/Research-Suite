from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.execution import (
    BackendUnavailableError,
    ProvisionedSandbox,
    SandboxRequest,
    build_sandbox_backend,
)
from backend.execution.backends.fake import FakeSandboxBackend


class SandboxRequestTypeTests(unittest.TestCase):
    def test_request_defaults(self) -> None:
        req = SandboxRequest(experiment_id="exp1", project_id="proj1", public_key="ssh-ed25519 AAA")
        self.assertEqual(req.cpu, 2.0)
        self.assertEqual(req.memory, 8192)
        self.assertEqual(req.time_limit, 3600)
        self.assertIsNone(req.gpu)
        self.assertEqual(req.image_packages, ())


class FakeSandboxBackendTests(unittest.TestCase):
    def test_acquire_returns_ssh_facts(self) -> None:
        backend = FakeSandboxBackend()
        provisioned = backend.acquire(
            request=SandboxRequest(
                experiment_id="exp1", project_id="proj1", public_key="ssh-ed25519 AAA", gpu="A100"
            )
        )
        self.assertIsInstance(provisioned, ProvisionedSandbox)
        self.assertTrue(provisioned.sandbox_id)
        self.assertTrue(provisioned.ssh_host)
        self.assertGreater(provisioned.ssh_port, 0)
        self.assertEqual(provisioned.ssh_user, "root")
        self.assertTrue(backend.is_alive(sandbox_id=provisioned.sandbox_id))

    def test_terminate_marks_dead(self) -> None:
        backend = FakeSandboxBackend()
        provisioned = backend.acquire(
            request=SandboxRequest(experiment_id="e", project_id="p", public_key="k")
        )
        self.assertTrue(backend.terminate(sandbox_id=provisioned.sandbox_id))
        self.assertFalse(backend.is_alive(sandbox_id=provisioned.sandbox_id))

    def test_transcript_roundtrip(self) -> None:
        backend = FakeSandboxBackend()
        backend.append_transcript(experiment_id="e", text="hello world\n")
        text = backend.read_transcript(
            sandbox_id="sb", experiment_id="e", volume_name="v", workdir="/w"
        )
        self.assertIn("hello world", text)


class FactoryTests(unittest.TestCase):
    def test_fake_selection(self) -> None:
        backend = build_sandbox_backend(repo_root=Path(tempfile.mkdtemp()), name="fake")
        self.assertEqual(backend.capabilities.name, "fake")

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(BackendUnavailableError):
            build_sandbox_backend(repo_root=Path(tempfile.mkdtemp()), name="nope")


if __name__ == "__main__":
    unittest.main()
