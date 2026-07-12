from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.dataplane.sandbox_outputs import pull_sandbox_outputs
from backend.utils import ValidationError


class SandboxOutputPullTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _sandbox(self) -> dict:
        return {
            "status": "running",
            "experiment_id": "exp_1",
            "sandbox_uid": "uid_123",
            "sandbox_id": "sb_1",
            "experiment_dir": "/workspace/experiments/exp-one",
            "local_experiment_dir": str(self.repo / "experiments" / "exp-one"),
            "ssh": {
                "host": "sandbox.example",
                "port": 2222,
                "user": "root",
                "key_path": str(self.repo / "key"),
            },
        }

    def test_default_pull_discovers_common_existing_outputs(self) -> None:
        calls: list[list[str]] = []

        def runner(command, **_kwargs):
            calls.append(list(command))
            if command[0] == "ssh":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="report.md\nresults/\n",
                    stderr="",
                )
            if command[0] == "rsync":
                source = str(command[-2])
                destination = Path(str(command[-1]))
                if "report.md" in source:
                    (destination / "report.md").write_text(
                        "## Summary\nRetained.\n",
                        encoding="utf-8",
                    )
                elif "results/" in source:
                    (destination / "metrics.json").write_text(
                        '{"accuracy": 0.72}\n',
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            raise AssertionError(command)

        result = pull_sandbox_outputs(
            repo_root=self.repo,
            sandbox=self._sandbox(),
            runner=runner,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["defaulted"])
        self.assertEqual(result["paths_requested"], ["report.md", "results/"])
        self.assertEqual(result["paths_pulled"], ["report.md", "results/"])
        self.assertEqual(result["destination_path"], "experiments/exp-one")
        self.assertEqual(result["files_present"], 2)
        self.assertGreater(result["bytes_present"], 0)
        self.assertEqual(result["files_kept_stale"], [])
        self.assertEqual(calls[0][0], "ssh")
        # Each path runs a stale-detection dry run, then the real transfer.
        rsyncs = [call for call in calls[1:] if call[0] == "rsync"]
        self.assertEqual(len(rsyncs), 4)
        self.assertIn("--dry-run", rsyncs[0])
        self.assertNotIn("--ignore-existing", rsyncs[0])
        self.assertIn("--ignore-existing", rsyncs[1])
        self.assertNotIn("--dry-run", rsyncs[1])
        for call in rsyncs:
            self.assertIn("--no-links", call)
        self.assertTrue((self.repo / "experiments" / "exp-one" / "report.md").exists())
        self.assertTrue(
            (self.repo / "experiments" / "exp-one" / "results" / "metrics.json").exists()
        )

    def test_existing_differing_file_is_kept_and_reported_stale(self) -> None:
        target = self.repo / "experiments" / "exp-one" / "report.md"
        target.parent.mkdir(parents=True)
        target.write_text("local report\n", encoding="utf-8")

        def runner(command, **_kwargs):
            assert command[0] == "rsync"
            if "--dry-run" in command:
                # The remote copy differs, so a plain rsync would replace it.
                return subprocess.CompletedProcess(
                    command, 0, stdout=">f.st...... report.md\n", stderr=""
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        result = pull_sandbox_outputs(
            repo_root=self.repo,
            sandbox=self._sandbox(),
            paths=["report.md"],
            runner=runner,
        )

        # The local file is kept — but the response says so instead of
        # reporting a clean success over stale bytes.
        self.assertTrue(result["ok"])
        self.assertEqual(result["files_transferred"], 0)
        self.assertEqual(result["files_kept_stale"], ["report.md"])
        self.assertEqual(target.read_text(encoding="utf-8"), "local report\n")

    def test_one_failing_path_does_not_hide_the_rest(self) -> None:
        def runner(command, **_kwargs):
            assert command[0] == "rsync"
            source = str(command[-2])
            if "graph.json" in source:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")
            if "--dry-run" not in command:
                destination = Path(str(command[-1]))
                (destination / "report.md").write_text("## Summary\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    command, 0, stdout=">f+++++++++ report.md\n", stderr=""
                )
            return subprocess.CompletedProcess(
                command, 0, stdout=">f+++++++++ report.md\n", stderr=""
            )

        result = pull_sandbox_outputs(
            repo_root=self.repo,
            sandbox=self._sandbox(),
            paths=["graph.json", "report.md"],
            runner=runner,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["paths_failed"], ["graph.json"])
        self.assertEqual(result["paths_pulled"], ["report.md"])
        self.assertEqual(result["files_transferred"], 1)
        self.assertIn("rsync from sandbox failed", result["errors"][0]["error"])

    def test_rejects_paths_that_escape_repo_semantics(self) -> None:
        with self.assertRaisesRegex(ValidationError, "may not contain"):
            pull_sandbox_outputs(
                repo_root=self.repo,
                sandbox=self._sandbox(),
                paths=["../secret.txt"],
                runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("runner should not be called")
                ),
            )


if __name__ == "__main__":
    unittest.main()
