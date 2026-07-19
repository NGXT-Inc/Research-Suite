"""Docker-simulated VM bootstrap integration (cloud plan Phase 5).

Applies the REAL Lambda-style phase-1 bootstrap (``build_bootstrap_core`` —
the exact fragment ``build_user_data`` ships) inside a small sshd container,
then exercises the dual-key contract end to end with the real backend code
paths:

  (a) a user-key SSH command goes through the rec.sh ForceCommand and is
      recorded to the transcript;
  (b) a management-key transcript read (``read_transcript``) works and is
      NOT recorded — the Match-exempt principal replaces the prefix bypass;

Skipped cleanly when docker is unavailable (a fast ``docker info`` probe).
The helper image is built once and cached as ``rp-test-sshd:bookworm``;
container state is shared across the ordered test methods.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from merv.brain.sandbox.execution.backends.lambda_labs.sandbox_backend import (
    LambdaLabsSandboxBackend,
)
from merv.brain.sandbox.execution.vm_bootstrap import build_bootstrap_core


IMAGE = "rp-test-sshd:bookworm"
EXPERIMENT_ID = "exp_t"
WORKDIR = "/workspace/exp_t"
SESSIONS_DIR = "/workspace/.merv_sessions/exp_t"
DATA_DIR = "/workspace/data"
TRANSCRIPT = f"{SESSIONS_DIR}/transcript.log"
MARKER = "hello-recorded-marker"

DOCKERFILE = """\
FROM debian:bookworm-slim
RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
       openssh-server sudo curl ca-certificates \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /run/sshd
CMD ["sleep", "infinity"]
"""


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


HAVE_DOCKER = _docker_available()


def _ensure_image() -> None:
    if (
        subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True
        ).returncode
        == 0
    ):
        return
    with tempfile.TemporaryDirectory() as context:
        (Path(context) / "Dockerfile").write_text(DOCKERFILE)
        subprocess.run(
            ["docker", "build", "-t", IMAGE, context],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )


@unittest.skipUnless(HAVE_DOCKER, "docker is not available")
class VmBootstrapDockerTest(unittest.TestCase):
    container: str = ""
    ssh_port: int = 0

    @classmethod
    def setUpClass(cls) -> None:
        # LambdaLabsSandboxBackend() validates a Lambda API key at construction.
        # The management-channel reads exercised here go over local SSH, not the
        # Lambda API, so a placeholder key is sufficient and keeps the test
        # self-contained (it must not depend on a real key leaking in from env).
        cls._saved_environ = dict(os.environ)
        os.environ.setdefault("RESEARCH_PLUGIN_LAMBDA_API_KEY", "test-placeholder")
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.user_key = base / "user_key"
        cls.mgmt_key = base / "mgmt_key"
        for key_path, comment in ((cls.user_key, "user"), (cls.mgmt_key, "mgmt")):
            subprocess.run(
                [
                    "ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                    "-C", f"rp-docker-{comment}", "-f", str(key_path),
                ],
                check=True,
                capture_output=True,
            )
        _ensure_image()
        run = subprocess.run(
            [
                "docker", "run", "-d", "--rm",
                "-p", "127.0.0.1:0:22",
                # Lets the container's curl reach the test's HTTP server on
                # the host (native-Linux docker needs the explicit mapping;
                # Docker Desktop ships the name anyway).
                "--add-host", "host.docker.internal:host-gateway",
                IMAGE, "sleep", "infinity",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.container = run.stdout.strip()
        port_line = subprocess.run(
            ["docker", "port", cls.container, "22/tcp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().splitlines()[0]
        cls.ssh_port = int(port_line.rsplit(":", 1)[1])
        # The REAL phase-1 bootstrap, exactly as build_user_data ships it.
        core = build_bootstrap_core(
            public_key=cls.user_key.with_suffix(".pub").read_text().strip(),
            management_public_key=cls.mgmt_key.with_suffix(".pub").read_text().strip(),
            experiment_id=EXPERIMENT_ID,
            workdir=WORKDIR,
            sessions_dir=SESSIONS_DIR,
            sandbox_data_dir=DATA_DIR,
        )
        bootstrap = (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            + core
            + "\n/usr/sbin/sshd || true\n"
        )
        cls._exec(bootstrap, check=True)
        cls._wait_for_mgmt_ssh()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.container:
            subprocess.run(
                ["docker", "rm", "-f", cls.container], capture_output=True
            )
        cls.tmp.cleanup()
        os.environ.clear()
        os.environ.update(cls._saved_environ)

    # ---------- helpers ----------

    @classmethod
    def _exec(
        cls, script: str, *, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", "-i", cls.container, "bash", "-s"],
            input=script,
            text=True,
            capture_output=True,
            timeout=120,
            check=check,
        )

    @classmethod
    def _ssh(cls, *, key: Path, user: str, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "ssh",
                "-i", str(key),
                "-p", str(cls.ssh_port),
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10",
                f"{user}@127.0.0.1",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

    @classmethod
    def _wait_for_mgmt_ssh(cls, timeout: float = 60.0) -> None:
        # Readiness is probed over the MANAGEMENT principal so the wait never
        # pollutes the transcript the recording assertions inspect.
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = cls._ssh(key=cls.mgmt_key, user="mervmgmt", command="true")
            if last.returncode == 0:
                return
            time.sleep(1.0)
        raise AssertionError(
            f"sshd in the container never became reachable: {last and last.stderr}"
        )

    def _transcript(self) -> str:
        result = self._exec(f"cat {TRANSCRIPT} 2>/dev/null || true")
        return result.stdout

    # ---------- the ordered flow ----------

    def test_01_user_key_command_is_recorded(self) -> None:
        result = self._ssh(
            key=self.user_key, user="root", command=f"echo {MARKER}"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(MARKER, result.stdout)
        log = self._transcript()
        self.assertIn(f"$ echo {MARKER}", log)
        self.assertIn("(exit 0)", log)

    def test_02_mgmt_transcript_read_works_and_is_unrecorded(self) -> None:
        backend = LambdaLabsSandboxBackend()
        tail = backend.read_transcript(
            sandbox_id="docker-vm",
            experiment_id=EXPERIMENT_ID,
            volume_name="",
            workdir=WORKDIR,
            ssh_host="127.0.0.1",
            ssh_port=self.ssh_port,
            ssh_user="root",  # ignored: the management channel has its own principal
            key_path=str(self.mgmt_key),
        )
        self.assertIn(MARKER, tail.data.decode("utf-8", "replace"))
        # The read itself never lands in the transcript: the Match-exempt
        # principal bypasses rec.sh, so polling cannot re-ingest the log.
        log = self._transcript()
        self.assertNotIn("tail -c", log)
        self.assertEqual(log.count("$ "), 1)
        # The management key cannot log in as the user principal (key
        # separation is real, not cosmetic).
        denied = self._ssh(key=self.mgmt_key, user="root", command="true")
        self.assertNotEqual(denied.returncode, 0)



if __name__ == "__main__":
    unittest.main()
