from __future__ import annotations

import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.execution import (
    BackendUnavailableError,
    BackendValidationError,
    JobExecutionPolicy,
    build_execution_backend,
)
from backend.execution.backends.modal import (
    ModalConfig,
    ModalExecutionBackend,
    decode_runtime_job_id,
    parse_modal_hints,
)
from backend.execution.backends.modal.runtime import ModalRuntime
from backend.execution.backends.modal.runner import (
    _command_script,
    _runner_script,
    cancel_runner,
    read_logs,
)
from backend.execution.backends.modal import _remote_runner
from backend.execution.backends.modal._sandbox_ops import (
    TRANSIENT_VOLUME_ERRORS,
    ensure_remote_dir,
    exec_checked,
)
from backend.execution.backends.modal.sync import (
    BaselineStore,
    SyncEngine,
    SyncResult,
)


class ModalConfigTests(unittest.TestCase):
    def test_env_file_loads_credentials_without_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("MODAL_TOKEN_ID=id_123\nMODAL_TOKEN_SECRET='secret_456'\n")
            with mock.patch.dict(
                os.environ,
                {"RESEARCH_PLUGIN_MODAL_ENV_FILE": str(env_file)},
                clear=True,
            ):
                config = ModalConfig.from_env()
                self.assertEqual(config.app_name, "research-plugin-jobs")
                self.assertEqual(config.runner_dir, "/workspace/repo/.research_plugin_job")
                self.assertEqual(os.environ["MODAL_TOKEN_ID"], "id_123")
                self.assertEqual(os.environ["MODAL_TOKEN_SECRET"], "secret_456")

    def test_hints_validate_gpu_and_modal_max_timeout_budget(self) -> None:
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=700,
            job_timeout=100,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )
        with self.assertRaises(BackendValidationError):
            parse_modal_hints(backend_hints={"gpu": "banana"}, config=config)
        # Longer jobs no longer need the default sandbox timeout to be large
        # enough; the runtime expands the sandbox timeout per job.
        hints = parse_modal_hints(backend_hints={"timeout": 200}, config=config)
        self.assertEqual(hints.timeout, 200)
        with self.assertRaises(BackendValidationError):
            parse_modal_hints(backend_hints={"timeout": 86_400}, config=config)

    def test_hints_default_to_a100_gpu(self) -> None:
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )

        hints = parse_modal_hints(backend_hints={}, config=config)

        self.assertEqual(hints.gpu, "A100")

    def test_hints_ignore_human_notes(self) -> None:
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )

        hints = parse_modal_hints(
            backend_hints={"notes": "operator-facing launch note"},
            config=config,
        )

        self.assertEqual(hints.gpu, "A100")

    def test_hints_reject_unscoped_experiment_path(self) -> None:
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )

        with self.assertRaises(BackendValidationError):
            parse_modal_hints(backend_hints={"experiment_path": "e001"}, config=config)
        with self.assertRaises(BackendValidationError):
            parse_modal_hints(backend_hints={"experiment_path": "."}, config=config)

    def test_config_rejects_top_level_remote_paths(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RESEARCH_PLUGIN_MODAL_WORKDIR": "/",
                "RESEARCH_PLUGIN_MODAL_RUNNER_DIR": "/workspace/.research_plugin_job",
            },
            clear=True,
        ):
            with self.assertRaises(BackendValidationError):
                ModalConfig.from_env()


class EnsureRemoteDirTests(unittest.TestCase):
    def test_ensure_remote_dir_uses_modal_sandbox_mkdir(self) -> None:
        sandbox = MkdirOnlySandbox()

        ensure_remote_dir(sandbox=sandbox, path="/workspace/.research_plugin_job")

        self.assertEqual(sandbox.mkdir_calls, [("/workspace/.research_plugin_job", True)])

    def test_ensure_remote_dir_prefers_filesystem_make_directory(self) -> None:
        sandbox = FilesystemMkdirSandbox()

        ensure_remote_dir(sandbox=sandbox, path="/workspace/.research_plugin_job")

        self.assertEqual(
            sandbox.filesystem.make_directory_calls,
            [("/workspace/.research_plugin_job", True)],
        )
        self.assertEqual(sandbox.mkdir_calls, [])


class ModalBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("print('ok')\n")
        (self.repo / "experiments" / "e001").mkdir(parents=True)
        (self.repo / "experiments" / "e001" / "script.py").write_text("print('draft')\n")
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
        self.tmp.cleanup()

    def test_submit_returns_decodable_runtime_id_without_modal_import(self) -> None:
        runtime = FakeRuntime(FakeSandbox("sb-test", FakeFilesystem(self.repo / "remote")))
        sync_engine = FakeSyncEngine(repo_root=self.repo)
        backend = ModalExecutionBackend(
            repo_root=self.repo,
            config=self.config,
            runtime=runtime,
            sync_engine=sync_engine,
            start_poller=False,
        )
        spec = JobExecutionPolicy(repo_root=self.repo).validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=["experiments/e001/results.json"],
            env=None,
            backend_hints={"experiment_path": "experiments/e001"},
            metadata={
                "research_plugin_job_id": "job_1",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
            },
        )
        with mock.patch(
            "backend.execution.backends.modal.submit_pipeline.write_runner_files"
        ) as write_runner:
            runtime_job_id = backend.submit(spec=spec)

        payload = decode_runtime_job_id(runtime_job_id)
        self.assertEqual(payload.sandbox_id, "sb-test")
        self.assertEqual(payload.job_id, "job_1")
        self.assertEqual(payload.experiment_id, "exp_1")
        self.assertEqual(payload.project_id, "proj_1")
        self.assertEqual(payload.volume_name, "research-plugin-proj_1")
        self.assertEqual(
            payload.runner_dir,
            "/workspace/.research_plugin_job/jobs/job_1",
        )
        self.assertEqual(sync_engine.ensure_calls, ["proj_1"])
        self.assertEqual(sync_engine.sync_calls, ["proj_1"])
        # Volume must be mounted writable at the remote workdir (no read_only filter).
        volumes = runtime.last_get_kwargs["volumes"]
        self.assertIn(self.config.remote_workdir, volumes)
        self.assertIn("research-plugin-proj_1", runtime.last_get_kwargs["compatibility_key"])
        write_runner.assert_called_once()
        # Runner must receive the volume name so it can commit on exit.
        kwargs = write_runner.call_args.kwargs
        self.assertEqual(kwargs.get("volume_name"), "research-plugin-proj_1")

    def test_submit_reports_runtime_id_before_writing_runner_files(self) -> None:
        runtime = FakeRuntime(FakeSandbox("sb-progress", FakeFilesystem(self.repo / "remote-progress")))
        backend = ModalExecutionBackend(
            repo_root=self.repo,
            config=self.config,
            runtime=runtime,
            sync_engine=FakeSyncEngine(repo_root=self.repo),
            start_poller=False,
        )
        spec = JobExecutionPolicy(repo_root=self.repo).validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=[],
            env=None,
            backend_hints={"experiment_path": "experiments/e001"},
            metadata={
                "research_plugin_job_id": "job_progress",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
            },
        )
        progress_events = []

        def progress(event):
            progress_events.append(event)

        def fail_write_runner(**_kwargs):
            self.assertTrue(
                any(event.runtime_job_id for event in progress_events),
                "runtime id should be reported before runner file writes",
            )
            raise RuntimeError("runner write blocked")

        with mock.patch(
            "backend.execution.backends.modal.submit_pipeline.write_runner_files",
            side_effect=fail_write_runner,
        ):
            with self.assertRaises(RuntimeError):
                backend.submit(spec=spec, progress=progress)

        runtime_ids = [event.runtime_job_id for event in progress_events if event.runtime_job_id]
        # The starting event fires after encoding, so the runtime id should be
        # visible before write_runner_files runs.
        self.assertGreaterEqual(len(runtime_ids), 1)
        self.assertEqual(len(set(runtime_ids)), 1, "all runtime_ids in progress should match")
        payload = decode_runtime_job_id(runtime_ids[0])
        self.assertEqual(payload.sandbox_id, "sb-progress")
        self.assertEqual(payload.job_id, "job_progress")

    def test_submit_terminates_sandbox_when_runner_write_fails(self) -> None:
        """Regression test: when write_runner_files raises (or anything between
        sandbox allocation and submit's return point), we MUST terminate the
        sandbox. Otherwise the user pays for a GPU that's still running the
        runner / training script for orphaned jobs.

        This was the bug that left three H100 sandboxes alive for hours on
        2026-05-27 after the IndexError-in-progress-callback failure.
        """
        sandbox = FakeSandbox("sb-orphan-candidate", FakeFilesystem(self.repo / "remote-orphan"))
        runtime = FakeRuntime(sandbox)
        activity_events: list[tuple[str, dict]] = []

        def record_activity(event_type, payload):
            activity_events.append((event_type, dict(payload)))

        backend = ModalExecutionBackend(
            repo_root=self.repo,
            config=self.config,
            runtime=runtime,
            sync_engine=FakeSyncEngine(repo_root=self.repo),
            activity=record_activity,
            start_poller=False,
        )
        spec = JobExecutionPolicy(repo_root=self.repo).validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=[],
            env=None,
            backend_hints={"experiment_path": "experiments/e001"},
            metadata={
                "research_plugin_job_id": "job_orphan",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
            },
        )

        with mock.patch(
            "backend.execution.backends.modal.submit_pipeline.write_runner_files",
            side_effect=RuntimeError("simulated runner write failure"),
        ):
            with self.assertRaises(RuntimeError):
                backend.submit(spec=spec)

        # Sandbox must have been torn down so it doesn't sit on a GPU.
        self.assertTrue(
            sandbox.terminated,
            "Modal backend must terminate the sandbox when submit fails after create",
        )
        self.assertTrue(sandbox.detached, "detach() should also be called for cleanliness")

        # Backend must drop the orphan from its in-memory sandbox map so a
        # subsequent backend.status() can't grab a dead handle by runtime_job_id.
        self.assertEqual(backend._sandboxes, {})

        # Cleanup must be observable in the activity log so users can correlate
        # mysterious "where did my sandbox go" later with the actual cause.
        cleanup_events = [
            (event_type, payload)
            for event_type, payload in activity_events
            if event_type == "modal.sandbox.terminated_on_submit_failure"
        ]
        self.assertEqual(len(cleanup_events), 1)
        _, payload = cleanup_events[0]
        self.assertEqual(payload["sandbox_id"], "sb-orphan-candidate")
        self.assertEqual(payload["reason"], "submit_failed_after_create")
        self.assertTrue(payload["runtime_job_id"])  # nonempty — submit allocated one before failing

    def test_submit_success_does_not_terminate_sandbox(self) -> None:
        """Companion to the failure test — happy path must NOT terminate the
        sandbox we just handed back via runtime_job_id."""
        sandbox = FakeSandbox("sb-keep-alive", FakeFilesystem(self.repo / "remote-keep"))
        runtime = FakeRuntime(sandbox)
        backend = ModalExecutionBackend(
            repo_root=self.repo,
            config=self.config,
            runtime=runtime,
            sync_engine=FakeSyncEngine(repo_root=self.repo),
            start_poller=False,
        )
        spec = JobExecutionPolicy(repo_root=self.repo).validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=[],
            env=None,
            backend_hints={"experiment_path": "experiments/e001"},
            metadata={
                "research_plugin_job_id": "job_keep",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
            },
        )
        with mock.patch(
            "backend.execution.backends.modal.submit_pipeline.write_runner_files"
        ):
            backend.submit(spec=spec)

        self.assertFalse(sandbox.terminated)
        self.assertFalse(sandbox.detached)

    def test_recover_runtime_job_id_from_modal_tags(self) -> None:
        sandbox = FakeSandbox("sb-recovered", FakeFilesystem(self.repo / "remote-recovered"))
        runtime = FakeRuntime(sandbox)
        backend = ModalExecutionBackend(
            repo_root=self.repo,
            config=self.config,
            runtime=runtime,
            sync_engine=FakeSyncEngine(repo_root=self.repo),
            start_poller=False,
        )

        runtime_job_id = backend.recover_runtime_job_id(
            job_id="job_recovered",
            project_id="proj_1",
            experiment_id="exp_1",
            backend_hints={"experiment_path": "experiments/e001", "timeout": 3000},
        )

        self.assertIsNotNone(runtime_job_id)
        payload = decode_runtime_job_id(runtime_job_id or "")
        self.assertEqual(payload.sandbox_id, "sb-recovered")
        self.assertEqual(payload.job_id, "job_recovered")
        self.assertEqual(payload.runner_dir, "/workspace/.research_plugin_job/jobs/job_recovered")
        self.assertEqual(
            runtime.last_list_tags,
            {
                "research_plugin": "true",
                "research_plugin_job_id": "job_recovered",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
            },
        )

    def test_reused_sandbox_resyncs_workspace_and_keeps_distinct_runner_dirs(self) -> None:
        runtime = FakeRuntime(FakeSandbox("sb-retry", FakeFilesystem(self.repo / "remote-retry")))
        sync_engine = FakeSyncEngine(repo_root=self.repo)
        backend = ModalExecutionBackend(
            repo_root=self.repo,
            config=self.config,
            runtime=runtime,
            sync_engine=sync_engine,
            start_poller=False,
        )
        spec_1 = JobExecutionPolicy(repo_root=self.repo).validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=["experiments/e001/results.json"],
            env=None,
            backend_hints={"experiment_path": "experiments/e001"},
            metadata={
                "research_plugin_job_id": "job_1",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
            },
        )
        spec_2 = JobExecutionPolicy(repo_root=self.repo).validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=["experiments/e001/results.json"],
            env=None,
            backend_hints={"experiment_path": "experiments/e001"},
            metadata={
                "research_plugin_job_id": "job_2",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
            },
        )

        with mock.patch(
            "backend.execution.backends.modal.submit_pipeline.write_runner_files"
        ):
            runtime_job_id_1 = backend.submit(spec=spec_1)
            runtime_job_id_2 = backend.submit(spec=spec_2)

        payload_1 = decode_runtime_job_id(runtime_job_id_1)
        payload_2 = decode_runtime_job_id(runtime_job_id_2)
        self.assertEqual(payload_1.sandbox_id, payload_2.sandbox_id)
        self.assertNotEqual(payload_1.runner_dir, payload_2.runner_dir)
        self.assertEqual(sync_engine.sync_calls, ["proj_1", "proj_1"])
        self.assertEqual(runtime.sandbox.reload_volume_calls, 2)

    def test_status_failed_retains_sandbox_for_retry_window(self) -> None:
        fs = FakeFilesystem(self.repo / "remote-status")
        fs.write_text(
            json.dumps({"state": "failed", "error": "boom", "started_at": None, "finished_at": "now"}),
            "/workspace/.research_plugin_job/status.json",
        )
        sandbox = FakeSandbox("sb-status", fs)
        runtime = FakeRuntime(sandbox)
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-status",
                "experiment_id": "exp_1",
                "remote_workdir": "/workspace/repo",
                "compatibility_key": ["H100", "default", False, [], None, None],
            }
        )

        status = backend.status(runtime_job_id=runtime_job_id)

        self.assertEqual(status.state, "failed")
        self.assertEqual(status.error, "boom")
        self.assertEqual(runtime.retained[0][1], "exp_1")

    def test_status_reports_queued_when_recovered_sandbox_is_not_ready(self) -> None:
        sandbox = PendingSandbox("sb-pending", FakeFilesystem(self.repo / "remote-pending"))
        runtime = FakeRuntime(sandbox)
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-pending",
                "runner_dir": "/workspace/.research_plugin_job",
                "remote_workdir": "/workspace/repo",
            }
        )

        status = backend.status(runtime_job_id=runtime_job_id)

        self.assertEqual(status.state, "queued")
        self.assertEqual(status.phase, "waiting_sandbox")
        self.assertIn("not ready", status.error or "")

    def test_status_uses_volume_committed_state_when_sandbox_finished_post_run(self) -> None:
        """Regression: a sandbox that already finished (e.g. idle timeout
        after the runner completed) makes _sandbox_ready_error raise. Before
        the fix we'd report queued.waiting_sandbox forever; now we read the
        runner's terminal status.json from the durable volume."""
        fs = FakeFilesystem(self.repo / "remote-finished-sandbox")
        sandbox = PendingSandbox("sb-finished", fs)
        runtime = FakeRuntime(sandbox)
        volume = runtime.volume_from_name("research-plugin-proj_1")
        volume._path(".research_plugin_job/jobs/job_1/status.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        volume._path(".research_plugin_job/jobs/job_1/status.json").write_text(
            json.dumps(
                {
                    "state": "succeeded",
                    "error": None,
                    "started_at": "start",
                    "finished_at": "finish",
                }
            )
        )
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-finished",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
                "volume_name": "research-plugin-proj_1",
                "runner_dir": "/workspace/repo/.research_plugin_job/jobs/job_1",
                "remote_workdir": "/workspace/repo",
                "compatibility_key": ["H100", "default", False, [], None, None],
            }
        )

        status = backend.status(runtime_job_id=runtime_job_id)

        self.assertEqual(status.state, "succeeded")
        self.assertEqual(status.started_at, "start")
        self.assertEqual(status.finished_at, "finish")

    def test_status_reports_failed_when_sandbox_terminated_without_committed_status(self) -> None:
        """A sandbox reaped mid-run (idle/hard timeout, preemption) leaves no
        terminal status.json on the Volume. Modal reports it finished via
        poll(); we must surface a failure rather than hang in
        queued.waiting_sandbox forever."""
        fs = FakeFilesystem(self.repo / "remote-reaped")
        sandbox = FinishedSandbox("sb-reaped", fs)
        runtime = FakeRuntime(sandbox)
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-reaped",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
                "volume_name": "research-plugin-proj_1",
                "runner_dir": "/workspace/repo/.research_plugin_job/jobs/job_1",
                "remote_workdir": "/workspace/repo",
                "compatibility_key": ["H100", "default", False, [], None, None],
            }
        )

        status = backend.status(runtime_job_id=runtime_job_id)

        self.assertEqual(status.state, "failed")
        self.assertIn("terminated before the job finished", status.error or "")

    def test_status_still_waiting_sandbox_when_not_ready_but_not_finished(self) -> None:
        """A cold-starting sandbox (not ready, poll() returns None) with no
        committed status must stay queued.waiting_sandbox — the dead-sandbox
        failure path must not fire for jobs that may still be starting."""
        fs = FakeFilesystem(self.repo / "remote-coldstart")
        sandbox = PendingSandbox("sb-coldstart", fs)
        runtime = FakeRuntime(sandbox)
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-coldstart",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
                "volume_name": "research-plugin-proj_1",
                "runner_dir": "/workspace/repo/.research_plugin_job/jobs/job_1",
                "remote_workdir": "/workspace/repo",
                "compatibility_key": ["H100", "default", False, [], None, None],
            }
        )

        status = backend.status(runtime_job_id=runtime_job_id)

        self.assertEqual(status.state, "queued")
        self.assertEqual(status.phase, "waiting_sandbox")

    def test_status_reports_runner_starting_when_status_json_still_says_queued(self) -> None:
        """Sandbox is ready and status.json exists, but the runner hasn't
        transitioned to state=running yet — the window between sandbox-ready
        and runner Popen. Should surface as queued.runner_starting so the UI
        can distinguish it from waiting_sandbox (Modal scheduler delay)."""
        fs = FakeFilesystem(self.repo / "remote-runner-starting")
        fs.write_text(
            json.dumps(
                {
                    "state": "queued",
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                }
            ),
            "/workspace/.research_plugin_job/status.json",
        )
        sandbox = FakeSandbox("sb-runner-starting", fs)
        runtime = FakeRuntime(sandbox)
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-runner-starting",
                "runner_dir": "/workspace/.research_plugin_job",
                "remote_workdir": "/workspace/repo",
            }
        )

        status = backend.status(runtime_job_id=runtime_job_id)

        self.assertEqual(status.state, "queued")
        self.assertEqual(status.phase, "runner_starting")

    def test_status_read_timeout_reports_running_without_error(self) -> None:
        sandbox = FakeSandbox("sb-slow-status", SlowReadFilesystem(self.repo / "remote-slow-status"))
        runtime = FakeRuntime(sandbox)
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-slow-status",
                "runner_dir": "/workspace/.research_plugin_job",
                "remote_workdir": "/workspace/repo",
            }
        )

        with mock.patch(
            "backend.execution.backends.modal.backend.SANDBOX_IO_TIMEOUT_SECONDS",
            0.01,
        ):
            status = backend.status(runtime_job_id=runtime_job_id)

        # A transient live-read timeout on a still-running job is an
        # operational gap, not a job error — it must not surface as status.error
        # (otherwise the UI shows a sticky timeout banner instead of the logs).
        self.assertEqual(status.state, "running")
        self.assertIsNone(status.error)

    def test_status_falls_back_to_committed_volume_status_when_sandbox_read_times_out(self) -> None:
        sandbox = FakeSandbox("sb-volume-status", SlowReadFilesystem(self.repo / "remote-volume-status"))
        runtime = FakeRuntime(sandbox)
        volume = runtime.volume_from_name("research-plugin-proj_1")
        volume._path(".research_plugin_job/jobs/job_1/status.json").parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        volume._path(".research_plugin_job/jobs/job_1/status.json").write_text(
            json.dumps(
                {
                    "state": "succeeded",
                    "error": None,
                    "started_at": "start",
                    "finished_at": "finish",
                }
            )
        )
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-volume-status",
                "project_id": "proj_1",
                "volume_name": "research-plugin-proj_1",
                "runner_dir": "/workspace/repo/.research_plugin_job/jobs/job_1",
                "remote_workdir": "/workspace/repo",
            }
        )

        with mock.patch(
            "backend.execution.backends.modal.backend.SANDBOX_IO_TIMEOUT_SECONDS",
            0.01,
        ):
            status = backend.status(runtime_job_id=runtime_job_id)

        self.assertEqual(status.state, "succeeded")
        self.assertEqual(status.started_at, "start")
        self.assertEqual(status.finished_at, "finish")
        # The daemon must reload its Volume handle to observe the sandbox's
        # background-committed status (a handle won't see another container's
        # commits otherwise).
        self.assertGreaterEqual(volume.reload_calls, 1)

    def test_logs_fall_back_to_committed_volume_logs_when_sandbox_read_times_out(self) -> None:
        sandbox = SlowExecSandbox("sb-volume-logs", FakeFilesystem(self.repo / "remote-volume-logs"))
        runtime = FakeRuntime(sandbox)
        volume = runtime.volume_from_name("research-plugin-proj_1")
        volume._path(".research_plugin_job/jobs/job_1/stdout.log").parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        volume._path(".research_plugin_job/jobs/job_1/stdout.log").write_text("stdout from volume")
        volume._path(".research_plugin_job/jobs/job_1/stderr.log").write_text("stderr from volume")
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-volume-logs",
                "project_id": "proj_1",
                "volume_name": "research-plugin-proj_1",
                "runner_dir": "/workspace/repo/.research_plugin_job/jobs/job_1",
                "remote_workdir": "/workspace/repo",
            }
        )

        with mock.patch(
            "backend.execution.backends.modal.backend.SANDBOX_IO_TIMEOUT_SECONDS",
            0.01,
        ):
            logs = backend.logs(runtime_job_id=runtime_job_id)

        self.assertIn("stdout from volume", logs)
        self.assertIn("stderr from volume", logs)

    def test_logs_fall_back_to_committed_volume_logs_when_sandbox_lookup_fails(self) -> None:
        runtime = FakeRuntime(FakeSandbox("sb-other", FakeFilesystem(self.repo / "remote-volume-logs-missing")))
        volume = runtime.volume_from_name("research-plugin-proj_1")
        volume._path(".research_plugin_job/jobs/job_1/stdout.log").parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        volume._path(".research_plugin_job/jobs/job_1/stdout.log").write_text("stdout after sandbox gone")
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-missing",
                "project_id": "proj_1",
                "volume_name": "research-plugin-proj_1",
                "runner_dir": "/workspace/repo/.research_plugin_job/jobs/job_1",
                "remote_workdir": "/workspace/repo",
            }
        )

        logs = backend.logs(runtime_job_id=runtime_job_id)

        self.assertIn("stdout after sandbox gone", logs)

    def test_cancel_retains_sandbox_when_cancel_runner_raises(self) -> None:
        runtime = FakeRuntime(FakeSandbox("sb-cancel-error", FakeFilesystem(self.repo / "remote-cancel")))
        backend = ModalExecutionBackend(repo_root=self.repo, config=self.config, runtime=runtime)
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-cancel-error",
                "experiment_id": "exp_1",
                "remote_workdir": "/workspace/repo",
                "runner_dir": "/workspace/.research_plugin_job/jobs/job_1",
                "compatibility_key": ["H100", "default", False, [], None, None],
            }
        )

        with mock.patch(
            "backend.execution.backends.modal.backend.cancel_runner",
            side_effect=RuntimeError("cancel failed"),
        ):
            with self.assertRaises(RuntimeError):
                backend.cancel(runtime_job_id=runtime_job_id)

        self.assertEqual(runtime.retained[0][1], "exp_1")

    def test_materialize_outputs_triggers_pull_sync_and_reports_local_presence(self) -> None:
        runtime = FakeRuntime(FakeSandbox("sb-materialize", FakeFilesystem(self.repo / "remote-materialize")))
        sync_engine = FakeSyncEngine(repo_root=self.repo)
        backend = ModalExecutionBackend(
            repo_root=self.repo,
            config=self.config,
            runtime=runtime,
            sync_engine=sync_engine,
            start_poller=False,
        )
        runtime_job_id = "modal:" + decode_safe(
            {
                "sandbox_id": "sb-materialize",
                "experiment_id": "exp_1",
                "project_id": "proj_1",
                "remote_workdir": "/workspace/repo",
                "runner_dir": "/workspace/.research_plugin_job/jobs/job_1",
                "volume_name": "research-plugin-proj_1",
                "compatibility_key": ["H100", "default", False, [], None, None],
            }
        )

        # Pretend the post-job pull already landed one of the declared outputs.
        results_path = self.repo / "experiments" / "e001" / "results.json"
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text("{}\n")

        outputs = backend.materialize_outputs(
            runtime_job_id=runtime_job_id,
            expected_outputs=[
                "experiments/e001/results.json",
                "experiments/e001/missing.json",
            ],
            repo_root=self.repo,
        )

        self.assertEqual(sync_engine.sync_calls, ["proj_1"])
        self.assertEqual(len(outputs), 2)
        present, absent = outputs
        self.assertEqual(present.path, "experiments/e001/results.json")
        self.assertTrue(present.exists)
        self.assertTrue(present.is_file)
        self.assertEqual(absent.path, "experiments/e001/missing.json")
        self.assertFalse(absent.exists)

        # A second materialize call must NOT re-trigger the pull (idempotent per job).
        backend.materialize_outputs(
            runtime_job_id=runtime_job_id,
            expected_outputs=["experiments/e001/results.json"],
            repo_root=self.repo,
        )
        self.assertEqual(sync_engine.sync_calls, ["proj_1"])

    def test_factory_defaults_to_modal_backend_without_importing_modal_sdk(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            backend = build_execution_backend(repo_root=self.repo)
        self.assertIsInstance(backend, ModalExecutionBackend)


class ModalRunnerTests(unittest.TestCase):
    def test_command_script_uses_strict_error_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            spec = JobExecutionPolicy(repo_root=repo).validate(
                command="python train.py",
                cwd=".",
                expected_outputs=["results.json"],
                env=None,
                backend_hints={},
                metadata={},
            )

        script = _command_script(spec=spec, remote_cwd="/workspace/repo")

        self.assertIn("set -euo pipefail", script)

    def test_cancel_runner_sets_sentinel_and_kills_process_group(self) -> None:
        fs = FakeFilesystem(Path(tempfile.mkdtemp(prefix="modal-cancel-runner-")))
        self.addCleanup(shutil.rmtree, fs.root, True)
        fs.write_text("12345", "/workspace/.research_plugin_job/pid")
        fs.write_text("22222", "/workspace/.research_plugin_job/supervisor_pid")
        sandbox = RecordingExecSandbox("sb-cancel", fs)
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )

        self.assertTrue(cancel_runner(sandbox=sandbox, config=config))

        # The sentinel, process-group kill, and fallback cancelled status are all
        # issued from one in-container shell, because the runner_dir is on the
        # mounted Volume where Modal's control-plane write API is rejected.
        self.assertEqual(sandbox.exec_calls[0][0:2], ("bash", "-c"))
        cmd = sandbox.exec_calls[0][-1]
        self.assertIn("echo 1 > /workspace/.research_plugin_job/cancel.requested", cmd)
        self.assertIn("kill -TERM -- -$pid", cmd)
        self.assertIn("/workspace/.research_plugin_job/status.json", cmd)
        self.assertIn("base64 -d", cmd)

    def test_read_logs_tails_remote_files(self) -> None:
        fs = FakeFilesystem(Path(tempfile.mkdtemp(prefix="modal-read-logs-")))
        self.addCleanup(shutil.rmtree, fs.root, True)
        stdout_text = "stdout-head" + ("a" * 60_000) + "stdout-tail"
        stderr_text = "stderr-head" + ("b" * 20_000) + "stderr-tail"
        fs.write_text(stdout_text, "/workspace/.research_plugin_job/stdout.log")
        fs.write_text(stderr_text, "/workspace/.research_plugin_job/stderr.log")
        sandbox = TailExecSandbox("sb-logs", fs)
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )

        logs = read_logs(sandbox=sandbox, config=config)

        self.assertNotIn("stdout-head", logs)
        self.assertNotIn("stderr-head", logs)
        self.assertIn("stdout-tail", logs)
        self.assertIn("stderr-tail", logs)
        self.assertTrue(all(call[0:2] == ("bash", "-c") for call in sandbox.exec_calls))

    def test_runner_script_reports_failed_when_popen_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runner = tmp_path / "runner.py"
            runner.write_text(_runner_script(), encoding="utf-8")
            status = tmp_path / "status.json"
            stdout = tmp_path / "stdout.log"
            stderr = tmp_path / "stderr.log"
            pid = tmp_path / "pid"
            cancel = tmp_path / "cancel.requested"

            result = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--status",
                    str(status),
                    "--stdout",
                    str(stdout),
                    "--stderr",
                    str(stderr),
                    "--pid",
                    str(pid),
                    "--cancel",
                    str(cancel),
                    "--timeout",
                    "5",
                    "--command",
                    str(tmp_path / "missing-command"),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )

            payload = json.loads(status.read_text(encoding="utf-8"))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(payload["state"], "failed")
            self.assertIn("FileNotFoundError", payload["error"])

    def test_runner_script_cancel_wins_over_child_termination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runner = tmp_path / "runner.py"
            runner.write_text(_runner_script(), encoding="utf-8")
            command = tmp_path / "sleep.sh"
            command.write_text("#!/usr/bin/env bash\nsleep 30\n", encoding="utf-8")
            command.chmod(0o755)
            status = tmp_path / "status.json"
            stdout = tmp_path / "stdout.log"
            stderr = tmp_path / "stderr.log"
            pid = tmp_path / "pid"
            cancel = tmp_path / "cancel.requested"

            process = subprocess.Popen(
                [
                    sys.executable,
                    str(runner),
                    "--status",
                    str(status),
                    "--stdout",
                    str(stdout),
                    "--stderr",
                    str(stderr),
                    "--pid",
                    str(pid),
                    "--cancel",
                    str(cancel),
                    "--timeout",
                    "30",
                    "--command",
                    str(command),
                ]
            )
            try:
                _wait_for_path(pid)
                cancel.write_text("1\n", encoding="utf-8")
                child_pid = int(pid.read_text(encoding="utf-8"))
                try:
                    os.killpg(os.getpgid(child_pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
                payload = json.loads(status.read_text(encoding="utf-8"))
                self.assertEqual(payload["state"], "cancelled")
            finally:
                if process.poll() is None:
                    process.kill()

    def test_remote_runner_final_status_is_written_before_volume_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status = tmp_path / "status.json"
            observed = {}

            def commit_probe(volume_mount, volume_name=None):
                observed["volume_mount"] = volume_mount
                observed["volume_name"] = volume_name
                observed["status_at_commit"] = json.loads(status.read_text(encoding="utf-8"))
                return None

            args = argparse_namespace(
                status=str(status),
                volume_mount=str(tmp_path),
                volume_name="research-plugin-proj_demo",
            )
            with mock.patch.object(_remote_runner, "commit_volume", side_effect=commit_probe):
                _remote_runner.finalize(
                    args,
                    {
                        "state": "succeeded",
                        "error": None,
                        "started_at": "start",
                        "finished_at": "finish",
                    },
                )

            self.assertEqual(observed["volume_mount"], str(tmp_path))
            self.assertEqual(observed["status_at_commit"]["state"], "succeeded")

    def test_remote_runner_commits_volume_with_mountpoint_sync(self) -> None:
        completed = subprocess.CompletedProcess(["sync", "/workspace/repo"], 0, "", "")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            error = _remote_runner.commit_volume("/workspace/repo")

        self.assertIsNone(error)
        run.assert_called_once_with(
            ["sync", "/workspace/repo"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

    def test_remote_runner_commit_failure_marks_status_failed_for_live_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status = tmp_path / "status.json"
            args = argparse_namespace(status=str(status), volume_mount=str(tmp_path))
            with mock.patch.object(
                _remote_runner,
                "commit_volume",
                return_value="RuntimeError: commit rejected",
            ):
                _remote_runner.finalize(
                    args,
                    {
                        "state": "succeeded",
                        "error": None,
                        "started_at": "start",
                        "finished_at": "finish",
                    },
                )

            payload = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "failed")
            self.assertIn("Modal volume commit failed", payload["error"])


class ModalRuntimeTests(unittest.TestCase):
    def test_runtime_sets_tags_after_create_without_create_kwargs_tags(self) -> None:
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )
        hints = parse_modal_hints(backend_hints={}, config=config)
        fake_modal = FakeModalModule()
        runtime = ModalRuntime(config=config, modal_module=fake_modal)

        with mock.patch.dict(
            os.environ,
            {"MODAL_TOKEN_ID": "id", "MODAL_TOKEN_SECRET": "secret"},
            clear=False,
        ):
            sandbox = runtime.get_or_create_sandbox(
                hints=hints,
                metadata={
                    "research_plugin_job_id": "job_1",
                    "experiment_id": "exp_1",
                    "project_id": "proj_1",
                },
            )

        self.assertNotIn("tags", fake_modal.Sandbox.last_kwargs)
        self.assertEqual(fake_modal.Sandbox.last_kwargs["gpu"], "A100")
        self.assertEqual(sandbox.tags["research_plugin"], "true")
        self.assertEqual(sandbox.tags["research_plugin_job_id"], "job_1")
        self.assertEqual(sandbox.tags["experiment_id"], "exp_1")
        self.assertEqual(sandbox.tags["project_id"], "proj_1")

    def test_runtime_refreshes_tags_when_reusing_retained_sandbox(self) -> None:
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )
        hints = parse_modal_hints(backend_hints={}, config=config)
        fake_modal = FakeModalModule()
        runtime = ModalRuntime(config=config, modal_module=fake_modal)

        with mock.patch.dict(
            os.environ,
            {"MODAL_TOKEN_ID": "id", "MODAL_TOKEN_SECRET": "secret"},
            clear=False,
        ):
            sandbox = runtime.get_or_create_sandbox(
                hints=hints,
                metadata={
                    "research_plugin_job_id": "job_1",
                    "experiment_id": "exp_1",
                    "project_id": "proj_1",
                },
            )
            runtime.retain_sandbox(
                sandbox=sandbox,
                experiment_id="exp_1",
                compatibility_key=hints.compatibility_key,
                delay_seconds=60,
            )
            with mock.patch.object(runtime, "_sandbox_alive", return_value=True):
                reused = runtime.get_or_create_sandbox(
                    hints=hints,
                    metadata={
                        "research_plugin_job_id": "job_2",
                        "experiment_id": "exp_1",
                        "project_id": "proj_1",
                    },
                )

        self.assertIs(reused, sandbox)
        self.assertEqual(sandbox.tags["research_plugin_job_id"], "job_2")
        self.assertEqual(sandbox.tags["experiment_id"], "exp_1")
        self.assertEqual(sandbox.tags["project_id"], "proj_1")
        self.assertIn("research_plugin_retained_until", sandbox.tags)

    def test_runtime_expands_sandbox_timeout_for_long_job_hint(self) -> None:
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )
        hints = parse_modal_hints(backend_hints={"timeout": 72_000}, config=config)
        fake_modal = FakeModalModule()
        runtime = ModalRuntime(config=config, modal_module=fake_modal)

        with mock.patch.dict(
            os.environ,
            {"MODAL_TOKEN_ID": "id", "MODAL_TOKEN_SECRET": "secret"},
            clear=False,
        ):
            runtime.get_or_create_sandbox(
                hints=hints,
                metadata={
                    "research_plugin_job_id": "job_long",
                    "experiment_id": "exp_1",
                    "project_id": "proj_1",
                },
            )

        self.assertEqual(fake_modal.Sandbox.last_kwargs["timeout"], 72_660)

    def test_sweeper_terminates_only_expired_retained_sandboxes(self) -> None:
        config = ModalConfig(
            app_name="test",
            retention_seconds=600,
            sandbox_timeout=4200,
            job_timeout=3000,
            idle_timeout=900,
            remote_workdir="/workspace/repo",
            runner_dir="/workspace/.research_plugin_job",
        )
        expired = FakeModalSandbox(
            tags={
                "research_plugin": "true",
                "research_plugin_retained_until": "900",
            }
        )
        retained = FakeModalSandbox(
            tags={
                "research_plugin": "true",
                "research_plugin_retained_until": "1100",
            }
        )
        untagged = FakeModalSandbox(tags={"research_plugin": "true"})
        fake_modal = FakeModalModule()
        fake_modal.Sandbox.list_items = [expired, retained, untagged]
        runtime = ModalRuntime(config=config, modal_module=fake_modal)

        with mock.patch.dict(
            os.environ,
            {"MODAL_TOKEN_ID": "id", "MODAL_TOKEN_SECRET": "secret"},
            clear=False,
        ):
            terminated = runtime.sweep_expired_retained_sandboxes(now=1000)

        self.assertEqual(terminated, 1)
        self.assertTrue(expired.terminated)
        self.assertTrue(expired.detached)
        self.assertFalse(retained.terminated)
        self.assertFalse(untagged.terminated)
        self.assertEqual(
            fake_modal.Sandbox.last_list_kwargs["tags"],
            {"research_plugin": "true"},
        )


class FakeStat:
    def __init__(self, kind: str) -> None:
        self.type = kind


class FakeFilesystem:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.copied_from_local_remote_paths = []
        self.root.mkdir(parents=True, exist_ok=True)

    def copy_from_local(self, local_path, remote_path: str):
        self.copied_from_local_remote_paths.append(remote_path)
        target = self._path(remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, target)

    def copy_to_local(self, remote_path: str, local_path):
        source = self._path(remote_path)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def make_directory(self, remote_path: str, create_parents: bool = True):
        self._path(remote_path).mkdir(parents=create_parents, exist_ok=True)

    def write_text(self, content: str, remote_path: str):
        path = self._path(remote_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def read_text(self, remote_path: str) -> str:
        return self._path(remote_path).read_text()

    def stat(self, remote_path: str) -> FakeStat:
        path = self._path(remote_path)
        if not path.exists():
            raise FileNotFoundError(remote_path)
        return FakeStat("directory" if path.is_dir() else "file")

    def _path(self, remote_path: str) -> Path:
        return self.root / remote_path.lstrip("/")


class FakeBatchUpload:
    def __init__(self, volume: "FakeVolume", force: bool) -> None:
        self.volume = volume
        self.force = force

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def put_file(self, local_file, remote_path: str) -> None:
        self.volume.uploaded.append(remote_path)
        target = self.volume._path(remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(local_file, "read"):
            target.write_bytes(local_file.read())
        else:
            shutil.copyfile(str(local_file), target)


class FakeVolume:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploaded = []
        self.removed = []
        self.mount_options = []
        self.reload_calls = 0

    def batch_upload(self, force: bool = False):
        return FakeBatchUpload(self, force)

    def read_file(self, path: str):
        target = self._path(path)
        if not target.exists():
            raise FileNotFoundError(path)
        yield target.read_bytes()

    def remove_file(self, path: str, recursive: bool = False) -> None:
        self.removed.append(path)
        target = self._path(path)
        if not target.exists():
            raise FileNotFoundError(path)
        target.unlink()

    def with_mount_options(self, *, read_only=None, sub_path=None):
        self.mount_options.append({"read_only": read_only, "sub_path": sub_path})
        return self

    def reload(self) -> None:
        self.reload_calls += 1

    def _path(self, path: str) -> Path:
        return self.root / path.lstrip("/")


class FakeSandbox:
    def __init__(self, object_id: str, filesystem: FakeFilesystem) -> None:
        self.object_id = object_id
        self.filesystem = filesystem
        self.reload_volume_calls = 0
        self.terminated = False
        self.detached = False

    def reload_volumes(self):
        self.reload_volume_calls += 1

    def terminate(self) -> None:
        self.terminated = True

    def detach(self) -> None:
        self.detached = True

    def exec(self, *args, timeout=None):
        command = args[-1]
        if "tar -xzf" in command and " -C " in command:
            tokens = shlex.split(command)
            if tokens[:2] == ["rm", "-rf"]:
                shutil.rmtree(self.filesystem._path(tokens[2]), ignore_errors=True)
            archive = tokens[tokens.index("-xzf") + 1]
            destination = tokens[tokens.index("-C") + 1]
            destination_path = self.filesystem._path(destination)
            destination_path.mkdir(parents=True, exist_ok=True)
            with tarfile.open(self.filesystem._path(archive), "r:gz") as tar:
                tar.extractall(destination_path, filter="data")
        return FakeProcess()


class RecordingExecSandbox(FakeSandbox):
    def __init__(self, object_id: str, filesystem: FakeFilesystem) -> None:
        super().__init__(object_id, filesystem)
        self.exec_calls = []

    def exec(self, *args, timeout=None):
        self.exec_calls.append(args)
        return FakeProcess()


class TailExecSandbox(RecordingExecSandbox):
    def exec(self, *args, timeout=None):
        self.exec_calls.append(args)
        command = args[-1]
        if "stdout.log" in command:
            return FakeProcess(
                stdout=self.filesystem.read_text("/workspace/.research_plugin_job/stdout.log")[-50_000:]
            )
        if "stderr.log" in command:
            return FakeProcess(
                stdout=self.filesystem.read_text("/workspace/.research_plugin_job/stderr.log")[-10_000:]
            )
        return FakeProcess()


class SlowExecSandbox(RecordingExecSandbox):
    def exec(self, *args, timeout=None):
        self.exec_calls.append(args)
        time.sleep(1)
        return FakeProcess()


class PendingSandbox(FakeSandbox):
    def wait_until_ready(self, *, timeout: int = 300) -> None:
        raise RuntimeError("pending capacity")


class FinishedSandbox(PendingSandbox):
    """Not ready (wait_until_ready raises) AND terminated (poll returns an exit
    code) — i.e. Modal reaped the sandbox before the runner committed status."""

    def poll(self):
        return 137


class SlowReadFilesystem(FakeFilesystem):
    def read_text(self, remote_path: str) -> str:
        time.sleep(1)
        return super().read_text(remote_path)


class FakeStream:
    def __init__(self, content: str) -> None:
        self.content = content

    def read(self) -> str:
        return self.content


class FakeProcess:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.exit_code = exit_code

    def wait(self) -> int:
        return self.exit_code


class FlakyExecSandbox:
    """exec() returns a failing FakeProcess for the first ``fail_times`` calls
    (with the given stderr), then a success. Records every command."""

    def __init__(self, *, fail_times: int, stderr: str) -> None:
        self.fail_times = fail_times
        self.stderr = stderr
        self.calls = 0

    def exec(self, *args, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            return FakeProcess(stderr=self.stderr, exit_code=1)
        return FakeProcess(exit_code=0)


class ExecCheckedRetryTests(unittest.TestCase):
    def test_retries_transient_volume_error_then_succeeds(self) -> None:
        sandbox = FlakyExecSandbox(
            fail_times=3,
            stderr="mkdir: cannot create directory '/workspace/repo/.research_plugin_job': Operation not permitted",
        )
        exec_checked(
            sandbox=sandbox,
            command="mkdir -p /workspace/repo/.research_plugin_job",
            timeout=60,
            retries=6,
            retry_on=TRANSIENT_VOLUME_ERRORS,
            retry_backoff_seconds=0,
        )
        self.assertEqual(sandbox.calls, 4)  # 3 transient failures + 1 success

    def test_gives_up_after_max_retries_on_persistent_transient_error(self) -> None:
        sandbox = FlakyExecSandbox(fail_times=99, stderr="Operation not permitted")
        with self.assertRaises(BackendUnavailableError):
            exec_checked(
                sandbox=sandbox,
                command="mkdir -p /x",
                timeout=60,
                retries=2,
                retry_on=TRANSIENT_VOLUME_ERRORS,
                retry_backoff_seconds=0,
            )
        self.assertEqual(sandbox.calls, 3)  # initial + 2 retries

    def test_non_retryable_error_raises_immediately(self) -> None:
        sandbox = FlakyExecSandbox(fail_times=99, stderr="No such file or directory")
        with self.assertRaises(BackendUnavailableError):
            exec_checked(
                sandbox=sandbox,
                command="mkdir -p /x",
                timeout=60,
                retries=6,
                retry_on=TRANSIENT_VOLUME_ERRORS,
                retry_backoff_seconds=0,
            )
        self.assertEqual(sandbox.calls, 1)  # no retries for a non-transient error


class MkdirOnlySandbox:
    def __init__(self) -> None:
        self.mkdir_calls = []

    def mkdir(self, path: str, parents: bool = False) -> None:
        self.mkdir_calls.append((path, parents))


class RecordingFilesystem:
    def __init__(self) -> None:
        self.make_directory_calls = []

    def make_directory(self, remote_path: str, *, create_parents: bool = True):
        self.make_directory_calls.append((remote_path, create_parents))


class FilesystemMkdirSandbox:
    def __init__(self) -> None:
        self.filesystem = RecordingFilesystem()
        self.mkdir_calls = []

    def mkdir(self, path: str, parents: bool = False) -> None:
        self.mkdir_calls.append((path, parents))


class FakeImage:
    def apt_install(self, *packages):
        return self

    def pip_install(self, *packages):
        return self

    def run_commands(self, *commands):
        return self


class FakeModalApp:
    app_id = "ap-test"


class FakeModalSandbox:
    object_id = "sb-created"

    def __init__(self, tags: dict[str, str] | None = None) -> None:
        self.tags = dict(tags or {})
        self.terminated = False
        self.detached = False

    def set_tags(self, tags: dict[str, str]) -> None:
        self.tags = dict(tags)

    def get_tags(self) -> dict[str, str]:
        return dict(self.tags)

    def terminate(self) -> None:
        self.terminated = True

    def detach(self) -> None:
        self.detached = True


class FakeModalModule:
    class App:
        @staticmethod
        def lookup(app_name: str, create_if_missing: bool = False):
            return FakeModalApp()

    class Image:
        @staticmethod
        def debian_slim(python_version: str):
            return FakeImage()

        @staticmethod
        def from_registry(image: str, add_python: str):
            return FakeImage()

    class Sandbox:
        last_kwargs = {}
        last_list_kwargs = {}
        list_items = []

        @classmethod
        def create(cls, **kwargs):
            if "tags" in kwargs:
                raise AssertionError("tags must be set after sandbox creation")
            cls.last_kwargs = kwargs
            return FakeModalSandbox()

        @staticmethod
        def from_id(sandbox_id: str):
            return FakeModalSandbox()

        @classmethod
        def list(cls, **kwargs):
            cls.last_list_kwargs = kwargs

            async def items():
                for item in cls.list_items:
                    yield item

            return items()


class FakeRuntime:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox
        self.retained = []
        self.last_get_kwargs = {}
        self.last_list_tags = {}
        self.volumes = {}

    def get_or_create_sandbox(self, **kwargs):
        self.last_get_kwargs = dict(kwargs)
        return self.sandbox

    def sandbox_from_id(self, sandbox_id: str):
        if sandbox_id != self.sandbox.object_id:
            raise RuntimeError("missing sandbox")
        return self.sandbox

    def list_sandboxes(self, *, tags=None):
        self.last_list_tags = dict(tags or {})
        if self.last_list_tags.get("research_plugin_job_id") == "missing":
            return []
        return [self.sandbox]

    def volume_from_name(self, volume_name: str):
        volume = self.volumes.get(volume_name)
        if volume is None:
            volume = FakeVolume(Path(tempfile.mkdtemp(prefix="modal-fake-volume-")))
            self.volumes[volume_name] = volume
        return volume

    def retain_sandbox(self, *, sandbox, experiment_id, compatibility_key, delay_seconds=None):
        self.retained.append((sandbox, experiment_id, compatibility_key, delay_seconds))

    def health(self):
        return {"ok": True, "name": "modal"}


class FakeBaseline:
    def __init__(self) -> None:
        self.known: list[str] = []
        self.registered: dict[str, dict] = {}
        self.polled: list[tuple[str, str]] = []
        self._conflicts: dict[str, set[str]] = {}

    def known_projects(self) -> list[str]:
        return list(self.known)

    def mark_polled(self, *, project_id: str, when: str) -> None:
        self.polled.append((project_id, when))

    def conflict_paths(self, *, project_id: str) -> set[str]:
        return set(self._conflicts.get(project_id, set()))

    def set_conflicts(self, *, project_id: str, paths: set[str]) -> None:
        """Test helper: pretend these paths are in conflict on this project."""
        self._conflicts[project_id] = set(paths)


class FakeSyncEngine:
    """Records sync calls without performing real volume IO."""

    def __init__(self, *, repo_root: Path, volume_name_pattern: str = "research-plugin-{project_id}") -> None:
        self.repo_root = Path(repo_root)
        self._pattern = volume_name_pattern
        self.baseline = FakeBaseline()
        self.ensure_calls: list[str] = []
        self.sync_calls: list[str] = []

    def volume_name(self, *, project_id: str) -> str:
        return self._pattern.format(project_id=project_id)

    def ensure_project_volume(self, *, project_id: str) -> dict[str, str]:
        self.ensure_calls.append(project_id)
        if project_id not in self.baseline.known:
            self.baseline.known.append(project_id)
        return {
            "project_id": project_id,
            "volume_name": self.volume_name(project_id=project_id),
            "mount_path": "/workspace/repo",
            "repo_dir": "",
        }

    def sync(self, *, project_id: str, skip_if_busy: bool = False) -> SyncResult:
        _ = skip_if_busy
        self.sync_calls.append(project_id)
        return SyncResult(project_id=project_id)


def decode_safe(payload: dict) -> str:
    from backend.execution.backends.modal import encode_runtime_job_id

    return encode_runtime_job_id(payload).removeprefix("modal:")


def argparse_namespace(**kwargs):
    return type("Args", (), kwargs)()


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


if __name__ == "__main__":
    unittest.main()
