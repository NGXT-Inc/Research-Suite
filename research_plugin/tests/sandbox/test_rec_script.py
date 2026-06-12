"""Functional tests for the rec.sh tmux-supervisor exec core.

These run the real ForceCommand wrapper script with bash against a temp
directory standing in for the sandbox filesystem. The contract under test:

- short commands stay synchronous: exact output bytes, real exit code
- transcript markers keep the parsed format: `[<ts>] $ <cmd>` / `[<ts>] (exit <rc>)`
- commands survive the foreground SSH wrapper being killed (the whole point)
- when tmux is missing or broken, execution falls back open to attached mode
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from backend.execution.backends.lambda_labs.sandbox_backend import (
    REC_SCRIPT as LAMBDA_REC_SCRIPT,
)
from backend.execution.backends.modal.sandbox_backend import (
    REC_SCRIPT as MODAL_REC_SCRIPT,
)
from backend.execution.bootstrap_tools import (
    BASELINE_APT_PACKAGES,
    LAMBDA_APT_PACKAGES,
    MODAL_APT_PACKAGES,
    REC_EXEC_CORE,
)

HAVE_TMUX = shutil.which("tmux") is not None


class RecScriptHarness(unittest.TestCase):
    """Run a REC_SCRIPT in a temp sandbox-like environment."""

    rec_script = LAMBDA_REC_SCRIPT

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workdir = root / "exp_t"
        self.data_dir = root / "data"
        self.sessions = root / ".research_plugin_sessions" / "exp_t"
        self.workdir.mkdir()
        self.data_dir.mkdir()
        self.sessions.mkdir(parents=True)
        self.script = root / "rec.sh"
        self.script.write_text(self.rec_script)
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @property
    def transcript(self) -> Path:
        return self.sessions / "transcript.log"

    def env(self, *, path: str | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            RP_WORKDIR=str(self.workdir),
            RP_EXPERIMENT_DIR=str(self.workdir),
            RP_SANDBOX_DATA_DIR=str(self.data_dir),
            RP_DASH_DIR=str(self.sessions),
            RP_EXPERIMENT_ID="exp_t",
        )
        if path is not None:
            env["PATH"] = path
        return env

    def run_rec(self, command: str, *, env: dict[str, str] | None = None, timeout: float = 30):
        full_env = env or self.env()
        full_env["SSH_ORIGINAL_COMMAND"] = command
        return subprocess.run(
            ["bash", str(self.script)],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def wait_for(self, predicate, *, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail("condition not met before timeout")


class TmuxSupervisorTest(RecScriptHarness):
    @unittest.skipUnless(HAVE_TMUX, "tmux not installed")
    def test_short_command_synchronous_output_and_exit_code(self) -> None:
        result = self.run_rec("echo hello-from-sandbox; exit 7")
        self.assertEqual(result.returncode, 7)
        self.assertIn("hello-from-sandbox", result.stdout)
        # The supervisor banner goes to stderr, never polluting stdout.
        self.assertIn("under tmux supervisor", result.stderr)
        self.assertNotIn("under tmux supervisor", result.stdout)
        log = self.transcript.read_text()
        self.assertIn("$ echo hello-from-sandbox; exit 7", log)
        self.assertIn("(exit 7)", log)

    @unittest.skipUnless(HAVE_TMUX, "tmux not installed")
    def test_run_dir_records_cmd_output_exit_code(self) -> None:
        self.run_rec("printf abc")
        runs = list((self.data_dir / ".rp_runs").iterdir())
        self.assertEqual(len(runs), 1)
        run_dir = runs[0]
        self.assertEqual((run_dir / "cmd").read_text(), "printf abc")
        self.assertEqual((run_dir / "out").read_text(), "abc")
        self.assertEqual((run_dir / "exit_code").read_text().strip(), "0")

    @unittest.skipUnless(HAVE_TMUX, "tmux not installed")
    def test_in_command_heredoc_still_works(self) -> None:
        result = self.run_rec('python3 - <<"PY"\nprint(6 * 7)\nPY')
        self.assertEqual(result.returncode, 0)
        self.assertIn("42", result.stdout)

    @unittest.skipUnless(HAVE_TMUX, "tmux not installed")
    def test_command_survives_foreground_kill(self) -> None:
        """Kill the SSH-side wrapper mid-run; the command must finish anyway."""
        env = self.env()
        env["SSH_ORIGINAL_COMMAND"] = "sleep 1; echo SURVIVED; exit 5"
        proc = subprocess.Popen(
            ["bash", str(self.script)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)  # past tmux launch, before the command completes
        proc.kill()
        proc.wait(timeout=5)
        # The tmux side keeps running and writes output + exit marker to the
        # transcript with nobody connected.
        self.wait_for(lambda: self.transcript.exists() and "SURVIVED" in self.transcript.read_text())
        self.wait_for(lambda: "(exit 5)" in self.transcript.read_text())
        runs = list((self.data_dir / ".rp_runs").iterdir())
        self.assertEqual((runs[0] / "exit_code").read_text().strip(), "5")

    def test_falls_back_attached_when_tmux_broken(self) -> None:
        """A tmux that cannot start sessions must not block execution."""
        shim_dir = Path(self.tmp.name) / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "tmux"
        shim.write_text("#!/bin/sh\nexit 1\n")
        shim.chmod(0o755)
        env = self.env(path=f"{shim_dir}:{os.environ['PATH']}")
        result = self.run_rec("echo fallback-ran; exit 3", env=env)
        self.assertEqual(result.returncode, 3)
        self.assertIn("fallback-ran", result.stdout)
        log = self.transcript.read_text()
        self.assertIn("(exit 3)", log)
        # No run dir: the supervisor path was never entered.
        self.assertEqual(list((self.data_dir / ".rp_runs").glob("*/exit_code")), [])


class ModalRecScriptTest(RecScriptHarness):
    rec_script = MODAL_REC_SCRIPT

    @unittest.skipUnless(HAVE_TMUX, "tmux not installed")
    def test_short_command_synchronous_output_and_exit_code(self) -> None:
        result = self.run_rec("echo modal-cmd; exit 4")
        self.assertEqual(result.returncode, 4)
        self.assertIn("modal-cmd", result.stdout)
        self.assertIn("(exit 4)", self.transcript.read_text())


class RecScriptContractTest(unittest.TestCase):
    def test_tmux_ships_in_both_backends_bootstrap(self) -> None:
        self.assertIn("tmux", BASELINE_APT_PACKAGES)
        self.assertIn("tmux", LAMBDA_APT_PACKAGES)
        self.assertIn("tmux", MODAL_APT_PACKAGES)

    def test_both_rec_scripts_embed_the_supervisor_core(self) -> None:
        for script in (LAMBDA_REC_SCRIPT, MODAL_REC_SCRIPT):
            self.assertIn(REC_EXEC_CORE, script)
            self.assertIn("tmux new-session", script)
            self.assertIn("rp_exec_attached", script)

    def test_modal_bypasses_file_transfer_protocols(self) -> None:
        self.assertIn(r"rsync\ --server*", MODAL_REC_SCRIPT)
        # Bypass must come before the supervisor core touches the command.
        self.assertLess(
            MODAL_REC_SCRIPT.index("rsync"),
            MODAL_REC_SCRIPT.index("tmux new-session"),
        )


if __name__ == "__main__":
    unittest.main()
