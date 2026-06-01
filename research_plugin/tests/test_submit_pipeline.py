"""Tests for the Modal SubmissionPipeline and its registry on the backend.

End-to-end submit behavior is covered by tests/test_modal_backend.py. These
tests pin down two contracts:

  1. SubmissionPipeline.status_report() correctly tracks live progress, even
     across threads, both for successful and failed runs.
  2. ModalExecutionBackend.live_submit_status(job_id=...) returns the live
     pipeline's report only while the submit is in flight, and None otherwise.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend.execution import JobExecutionPolicy
from backend.execution import SubmitStatusReport
from backend.execution.backends.modal import ModalConfig, ModalExecutionBackend
from backend.execution.backends.modal.submit_pipeline import SubmissionPipeline

from tests.test_modal_backend import (
    FakeFilesystem,
    FakeRuntime,
    FakeSandbox,
    FakeSyncEngine,
)


EXPECTED_STAGES: tuple[str, ...] = (
    "preparing",
    "volume",
    "syncing",
    "conflict_gate",
    "acquiring_sandbox",
    "encoding",
    "starting",
)


class _Fixture(unittest.TestCase):
    """Shared setup: a repo, a Modal config, and helpers to build backends/specs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("print('ok')\n")
        (self.repo / "experiments" / "e001").mkdir(parents=True)
        (self.repo / "experiments" / "e001" / "script.py").write_text("")
        self.config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_backend(self, *, sandbox_id: str = "sb-test") -> ModalExecutionBackend:
        runtime = FakeRuntime(FakeSandbox(sandbox_id, FakeFilesystem(self.repo / "remote")))
        return ModalExecutionBackend(
            repo_root=self.repo,
            config=self.config,
            runtime=runtime,
            sync_engine=FakeSyncEngine(repo_root=self.repo),
            start_poller=False,
        )

    def _build_spec(self, *, job_id: str = "job_1"):
        return JobExecutionPolicy(repo_root=self.repo).validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=[],
            env=None,
            backend_hints={"experiment_path": "experiments/e001"},
            metadata={
                "research_plugin_job_id": job_id,
                "experiment_id": "exp_1",
                "project_id": "proj_1",
            },
        )


class SubmissionPipelineStatusReportTests(_Fixture):
    """Pipeline-level contract: status_report() tracks live progress."""

    def test_initial_report_lists_all_stages_with_no_progress(self) -> None:
        backend = self._build_backend()
        pipeline = SubmissionPipeline(backend=backend)

        report = pipeline.status_report()

        self.assertIsInstance(report, SubmitStatusReport)
        self.assertEqual(report.stages, EXPECTED_STAGES)
        self.assertIsNone(report.current)
        self.assertEqual(report.completed, ())
        self.assertIsNone(report.failed_at)
        self.assertEqual(report.runtime_job_id, "")

    def test_report_after_successful_run_shows_every_stage_completed(self) -> None:
        backend = self._build_backend()
        pipeline = SubmissionPipeline(backend=backend)
        with mock.patch(
            "backend.execution.backends.modal.submit_pipeline.write_runner_files"
        ):
            pipeline.run(spec=self._build_spec())

        report = pipeline.status_report()
        self.assertIsNone(report.current)
        self.assertEqual(report.completed, EXPECTED_STAGES)
        self.assertIsNone(report.failed_at)
        self.assertTrue(
            report.runtime_job_id.startswith("modal:"),
            f"runtime_job_id should be filled by encoding stage; got {report.runtime_job_id!r}",
        )

    def test_report_after_failure_pins_failed_at_to_the_raising_stage(self) -> None:
        backend = self._build_backend()
        pipeline = SubmissionPipeline(backend=backend)
        with mock.patch(
            "backend.execution.backends.modal.submit_pipeline.write_runner_files",
            side_effect=RuntimeError("simulated runner write failure"),
        ):
            with self.assertRaises(RuntimeError):
                pipeline.run(spec=self._build_spec())

        report = pipeline.status_report()
        self.assertIsNone(report.current)
        self.assertEqual(report.failed_at, "starting")
        # Every stage before 'starting' must be in completed; 'starting' itself
        # is the failing stage and must not be marked completed.
        self.assertEqual(report.completed, EXPECTED_STAGES[:-1])

    def test_mid_stage_report_reflects_the_currently_running_stage(self) -> None:
        """A foreign thread sees current=<stage> while that stage executes,
        and the lock around _current must not block waiting for the slow
        stage to finish."""
        backend = self._build_backend()
        pipeline = SubmissionPipeline(backend=backend)

        enter_stage = threading.Event()
        release_stage = threading.Event()

        def blocking_write_runner(**_):
            enter_stage.set()
            if not release_stage.wait(timeout=5):
                raise AssertionError("test did not release the blocking stage in time")

        outcome: list[object] = []

        def run_in_worker() -> None:
            try:
                with mock.patch(
                    "backend.execution.backends.modal.submit_pipeline.write_runner_files",
                    side_effect=blocking_write_runner,
                ):
                    pipeline.run(spec=self._build_spec())
                outcome.append("ok")
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        worker = threading.Thread(target=run_in_worker, daemon=True)
        worker.start()
        try:
            self.assertTrue(
                enter_stage.wait(timeout=5),
                "pipeline never reached the starting stage's write_runner_files call",
            )
            report = pipeline.status_report()
            self.assertEqual(report.current, "starting")
            self.assertEqual(report.completed, EXPECTED_STAGES[:-1])
            self.assertIsNone(report.failed_at)
            self.assertTrue(report.runtime_job_id.startswith("modal:"))
        finally:
            release_stage.set()
            worker.join(timeout=5)
        self.assertEqual(outcome, ["ok"])


class LiveSubmitStatusRegistryTests(_Fixture):
    """Backend-level contract: live_submit_status() returns the in-flight
    pipeline's report by job_id, and only while the submit is in flight."""

    def test_live_submit_status_is_none_before_any_submit(self) -> None:
        backend = self._build_backend()
        self.assertIsNone(backend.live_submit_status(job_id="job_1"))

    def test_live_submit_status_is_none_after_submit_completes(self) -> None:
        backend = self._build_backend()
        with mock.patch(
            "backend.execution.backends.modal.submit_pipeline.write_runner_files"
        ):
            backend.submit(spec=self._build_spec(job_id="job_done"))
        self.assertIsNone(backend.live_submit_status(job_id="job_done"))

    def test_live_submit_status_returns_in_flight_pipeline_report(self) -> None:
        """While a submit is mid-stage, live_submit_status returns the live
        report; once the stage releases and the submit returns, the registry
        clears."""
        backend = self._build_backend()

        enter_stage = threading.Event()
        release_stage = threading.Event()

        def blocking_write_runner(**_):
            enter_stage.set()
            if not release_stage.wait(timeout=5):
                raise AssertionError("test did not release the blocking stage in time")

        def submit_in_worker() -> None:
            with mock.patch(
                "backend.execution.backends.modal.submit_pipeline.write_runner_files",
                side_effect=blocking_write_runner,
            ):
                backend.submit(spec=self._build_spec(job_id="job_inflight"))

        worker = threading.Thread(target=submit_in_worker, daemon=True)
        worker.start()
        try:
            self.assertTrue(enter_stage.wait(timeout=5))
            report = backend.live_submit_status(job_id="job_inflight")
            self.assertIsNotNone(report)
            assert report is not None  # for the type checker
            self.assertEqual(report.current, "starting")
            # Looking up a different job returns None — registry is per-job_id.
            self.assertIsNone(backend.live_submit_status(job_id="someone_else"))
        finally:
            release_stage.set()
            worker.join(timeout=5)

        # Registry cleared once submit returned.
        self.assertIsNone(backend.live_submit_status(job_id="job_inflight"))


if __name__ == "__main__":
    unittest.main()
