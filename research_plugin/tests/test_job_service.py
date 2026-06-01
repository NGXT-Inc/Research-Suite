from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import ResearchPluginApp
from backend.utils import NotFoundError, PermissionDeniedError
from backend.execution import BackendCapabilities
from backend.execution.backends.fake import FakeBackend


class JobServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def ready_experiment(self):
        project = self.call("project.create", name="Job Test")
        claim = self.call("claim.create", project_id=project["id"], statement="Job output should exist.")
        exp = self.call("experiment.create", project_id=project["id"], intent="Run a backend script.", tested_claim_ids=[claim["id"]])
        (self.repo / "plan.md").write_text("metric: output exists\n")
        plan = self.call("resource.register_file", project_id=project["id"], path="plan.md", kind="note")
        self.call("resource.associate", project_id=project["id"], resource_id=plan["id"], target_type="experiment", target_id=exp["id"], role="plan")
        self.call("experiment.transition", project_id=project["id"], experiment_id=exp["id"], transition="submit_design")
        req = self.call("review.request", project_id=project["id"], target_type="experiment", target_id=exp["id"], role="design_reviewer")
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id="design-reviewer",
        )
        self.call("review.submit", review_session_id=session["review_session_id"], verdict="pass", notes="ok")
        self.call("experiment.transition", project_id=project["id"], experiment_id=exp["id"], transition="mark_ready_to_run")
        return project, exp

    def test_submit_reconcile_logs_outputs_and_workflow(self) -> None:
        project, exp = self.ready_experiment()
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "train.py").write_text("print('training')\n")
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python scripts/train.py",
            expected_outputs=["experiments/e001/results.json"],
            backend_hints={"queue": "test"},
        )
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["outputs"], [{"path": "experiments/e001/results.json", "exists": False}])
        self.assertEqual(self.call("experiment.get_state", project_id=project["id"], experiment_id=exp["id"])["status"], "running")
        runtime_id = self.backend.last_runtime_job_id
        self.assertEqual(dict(self.backend.submitted[runtime_id].backend_hints), {"queue": "test"})

        self.backend.set_status(runtime_job_id=runtime_id, state="running")
        running = self.call("job.status", project_id=project["id"], job_id=job["id"])
        self.assertEqual(running["status"], "running")
        status = self.call("workflow.status_and_next", project_id=project["id"], experiment_id=exp["id"])
        self.assertEqual(status["workflow"]["next_action"], "wait_for_job")

        self.backend.set_status(runtime_job_id=runtime_id, state="succeeded")
        self.backend.logs_by_id[runtime_id] = "submitted\nfinished\n"
        done = self.call("job.status", project_id=project["id"], job_id=job["id"])
        self.assertEqual(done["status"], "succeeded")
        # Non-materializing backends skip the round trip but still stamp the job.
        ui_status = self.app.jobs.get_status_for_ui(
            project_id=project["id"], job_id=job["id"], reconcile=False
        )
        self.assertIsNotNone(ui_status["materialized_at"])
        self.assertEqual(self.backend.materialize_calls, [])
        self.assertEqual(done["outputs"][0]["path"], "experiments/e001/results.json")
        self.assertFalse(done["outputs"][0]["exists"])
        logs = self.call("job.logs", project_id=project["id"], job_id=job["id"], tail=1)
        self.assertEqual(logs["logs"], "finished")

    def test_workflow_status_reports_last_known_job_state_without_polling_backend(self) -> None:
        project, exp = self.ready_experiment()
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "train.py").write_text("print('training')\n")
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python scripts/train.py",
            expected_outputs=["experiments/e001/results.json"],
        )
        runtime_id = self.backend.last_runtime_job_id
        self.backend.set_status(runtime_job_id=runtime_id, state="succeeded")

        status = self.call("workflow.status_and_next", project_id=project["id"], experiment_id=exp["id"])

        self.assertEqual(status["jobs"][0]["status"], "queued")
        self.assertEqual(status["workflow"]["next_action"], "wait_for_job")
        refreshed = self.call("job.status", project_id=project["id"], job_id=job["id"])
        self.assertEqual(refreshed["status"], "succeeded")

    def test_successful_retry_takes_precedence_over_stale_failed_job(self) -> None:
        project, exp = self.ready_experiment()
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "train.py").write_text("print('training')\n")
        expected_outputs = ["experiments/e001/results.json", "experiments/e001/report.md"]

        failed = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python scripts/train.py",
            expected_outputs=expected_outputs,
        )
        failed_runtime_id = self.backend.last_runtime_job_id
        self.backend.set_status(runtime_job_id=failed_runtime_id, state="failed")
        self.call("job.status", project_id=project["id"], job_id=failed["id"])
        failed_status = self.call("workflow.status_and_next", project_id=project["id"], experiment_id=exp["id"])
        self.assertEqual(failed_status["workflow"]["next_action"], "inspect_job_failure")
        self.assertIn("job.submit", failed_status["workflow"]["allowed_actions"])

        retry = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python scripts/train.py",
            expected_outputs=expected_outputs,
        )
        retry_runtime_id = self.backend.last_runtime_job_id
        self.backend.set_status(runtime_job_id=retry_runtime_id, state="succeeded")
        self.call("job.status", project_id=project["id"], job_id=retry["id"])

        retry_status = self.call("workflow.status_and_next", project_id=project["id"], experiment_id=exp["id"])
        self.assertEqual(retry_status["workflow"]["current_gate"], "result_sync_required")
        self.assertEqual(retry_status["workflow"]["next_action"], "sync_result_resources")
        self.assertEqual(retry_status["workflow"]["resource_guidance"]["association_role"], "result")
        self.assertEqual(retry_status["workflow"]["resource_guidance"]["expected_output_paths"], expected_outputs)
        self.assertEqual(retry_status["workflow"]["resource_guidance"]["job_id"], retry["id"])

    def test_job_status_rejects_wrong_project_before_polling_backend(self) -> None:
        project, exp = self.ready_experiment()
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
        )
        other_project = self.call("project.create", name="Other Project")

        with self.assertRaises(NotFoundError):
            self.call("job.status", project_id=other_project["id"], job_id=job["id"])

        self.assertEqual(self.backend.status_calls, [])

    def test_logs_reject_wrong_project_before_polling_backend(self) -> None:
        project, exp = self.ready_experiment()
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
        )
        other_project = self.call("project.create", name="Other Project")

        with self.assertRaises(NotFoundError):
            self.call("job.logs", project_id=other_project["id"], job_id=job["id"])

        # reconcile (and therefore backend polling) must not have run for a
        # job the caller does not own.
        self.assertEqual(self.backend.status_calls, [])

    def test_submit_rejects_before_ready_and_rejects_shell_control(self) -> None:
        project = self.call("project.create", name="Not Ready Project")
        exp = self.call("experiment.create", project_id=project["id"], intent="Not ready")
        with self.assertRaises(PermissionDeniedError):
            self.call("job.submit", project_id=project["id"], experiment_id=exp["id"], command="python train.py")
        ready_project, ready = self.ready_experiment()
        with self.assertRaises(PermissionDeniedError):
            self.call("job.submit", project_id=ready_project["id"], experiment_id=ready["id"], command="python train.py && rm -rf .")

    def test_logs_tail_zero_returns_empty_string(self) -> None:
        project, exp = self.ready_experiment()
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
        )
        runtime_id = self.backend.last_runtime_job_id
        self.backend.logs_by_id[runtime_id] = "line1\nline2\nline3\n"
        self.assertEqual(
            self.call("job.logs", project_id=project["id"], job_id=job["id"], tail=0)["logs"],
            "",
        )
        self.assertEqual(
            self.call("job.logs", project_id=project["id"], job_id=job["id"], tail=2)["logs"],
            "line2\nline3",
        )

    def test_submit_lifts_misplaced_env_and_drops_note_hint(self) -> None:
        project, exp = self.ready_experiment()
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "train.py").write_text("print('training')\n")

        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python scripts/train.py",
            backend_hints={
                "env": {"PYTHONUNBUFFERED": "1"},
                "note": "conversation-only context",
                "queue": "test",
            },
        )

        runtime_id = self.backend.last_runtime_job_id
        self.assertEqual(job["status"], "queued")
        self.assertEqual(dict(self.backend.submitted[runtime_id].env), {"PYTHONUNBUFFERED": "1"})
        self.assertEqual(dict(self.backend.submitted[runtime_id].backend_hints), {"queue": "test"})

    def test_modal_submit_returns_before_backend_submit_finishes(self) -> None:
        class BlockingModalBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__(materializes=True)
                self.capabilities = BackendCapabilities(
                    name="modal",
                    supports_local_working_dir=False,
                    materializes_outputs=True,
                )
                self.started = threading.Event()
                self.release = threading.Event()

            def submit(self, *, spec, progress=None):
                if progress is not None:
                    from backend.execution import ExecutionProgress

                    progress(ExecutionProgress(phase="syncing", message="Preparing execution"))
                self.started.set()
                self.release.wait(timeout=5)
                return super().submit(spec=spec, progress=progress)

        backend = BlockingModalBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "async.sqlite",
            execution_backend=backend,
        )
        project, exp = self.ready_experiment()

        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
        )

        self.addCleanup(backend.release.set)
        self.assertEqual(job["status"], "submitting")
        self.assertTrue(backend.started.wait(timeout=1))
        status = self.call("job.status", project_id=project["id"], job_id=job["id"])
        self.assertEqual(status["status"], "submitting")
        self.assertEqual(status["message"], "Preparing execution")
        backend.release.set()
        deadline = time.time() + 2
        status = job
        while time.time() < deadline:
            status = self.call("job.status", project_id=project["id"], job_id=job["id"])
            if status["status"] == "queued":
                break
            time.sleep(0.05)
        self.assertEqual(status["status"], "queued")

    def test_modal_submit_allows_second_active_project_job(self) -> None:
        class BlockingModalBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__(materializes=True)
                self.capabilities = BackendCapabilities(
                    name="modal",
                    supports_local_working_dir=False,
                    materializes_outputs=True,
                )
                self.started_once = threading.Event()
                self.started_twice = threading.Event()
                self.release = threading.Event()
                self._lock = threading.Lock()
                self.started_count = 0

            def submit(self, *, spec, progress=None):
                with self._lock:
                    self.started_count += 1
                    if self.started_count == 1:
                        self.started_once.set()
                    if self.started_count == 2:
                        self.started_twice.set()
                self.release.wait(timeout=5)
                return super().submit(spec=spec, progress=progress)

        backend = BlockingModalBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "active_modal.sqlite",
            execution_backend=backend,
        )
        project, exp = self.ready_experiment()

        first = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
        )

        self.addCleanup(backend.release.set)
        self.assertEqual(first["status"], "submitting")
        self.assertTrue(backend.started_once.wait(timeout=1))

        second = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
        )

        self.assertEqual(second["status"], "submitting")
        self.assertTrue(backend.started_twice.wait(timeout=1))
        jobs = self.call("job.list", project_id=project["id"], status="submitting")["jobs"]
        self.assertEqual({job["id"] for job in jobs}, {first["id"], second["id"]})

        backend.release.set()
        deadline = time.time() + 2
        statuses = {first["id"]: first, second["id"]: second}
        while time.time() < deadline:
            statuses = {
                job_id: self.call("job.status", project_id=project["id"], job_id=job_id)
                for job_id in statuses
            }
            if all(status["status"] == "queued" for status in statuses.values()):
                break
            time.sleep(0.05)
        self.assertEqual({status["status"] for status in statuses.values()}, {"queued"})

    def test_stale_modal_submission_without_runtime_id_fails_on_status(self) -> None:
        project, exp = self.ready_experiment()
        old = "2000-01-01T00:00:00Z"
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                  id, project_id, experiment_id, backend, command, cwd,
                  expected_outputs_json, backend_hints_json, metadata_json,
                  status, progress_phase, progress_message, progress_updated_at,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job_stale_modal_submit",
                    project["id"],
                    exp["id"],
                    "modal",
                    "python train.py",
                    ".",
                    "[]",
                    "{}",
                    "{}",
                    "submitting",
                    "starting",
                    "Starting execution",
                    old,
                    old,
                    old,
                ),
            )

        with patch.dict("os.environ", {"RESEARCH_PLUGIN_MODAL_SUBMIT_STALE_SECONDS": "1"}):
            status = self.call(
                "job.status",
                project_id=project["id"],
                job_id="job_stale_modal_submit",
            )

        self.assertEqual(status["status"], "failed")
        self.assertIn("Modal submission did not finish", status["error"])

    def test_stale_modal_submission_owned_by_live_process_fails_on_status(self) -> None:
        project, exp = self.ready_experiment()
        old = "2000-01-01T00:00:00Z"
        other_pid = os.getpid() + 100000
        metadata = {
            "submit_owner_pid": str(other_pid),
            "submit_worker_name": "research-plugin-submit-job_cross_process",
        }
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                  id, project_id, experiment_id, backend, command, cwd,
                  expected_outputs_json, backend_hints_json, metadata_json,
                  status, progress_phase, progress_message, progress_updated_at,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job_cross_process",
                    project["id"],
                    exp["id"],
                    "modal",
                    "python train.py",
                    ".",
                    "[]",
                    "{}",
                    json.dumps(metadata, sort_keys=True),
                    "submitting",
                    "starting",
                    "Starting execution",
                    old,
                    old,
                    old,
                ),
            )

        with (
            patch.dict("os.environ", {"RESEARCH_PLUGIN_MODAL_SUBMIT_STALE_SECONDS": "1"}),
            patch("backend.services.jobs._process_alive", return_value=True),
        ):
            status = self.call(
                "job.status",
                project_id=project["id"],
                job_id="job_cross_process",
            )

        self.assertEqual(status["status"], "failed")
        self.assertIn("did not publish a runtime job id", status["error"])

    def test_modal_submission_without_runtime_id_recovers_from_backend_tags(self) -> None:
        class RecoveringModalBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__(materializes=True)
                self.capabilities = BackendCapabilities(
                    name="modal",
                    supports_local_working_dir=False,
                    materializes_outputs=True,
                )
                self.recovered = []

            def recover_runtime_job_id(self, **kwargs):
                self.recovered.append(kwargs)
                runtime_job_id = "modal:recovered"
                self.submitted[runtime_job_id] = None
                self.statuses[runtime_job_id] = "queued"
                self.logs_by_id[runtime_job_id] = "recovered\n"
                return runtime_job_id

        backend = RecoveringModalBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "recover.sqlite",
            execution_backend=backend,
        )
        project, exp = self.ready_experiment()
        now = "2026-05-27T00:00:00Z"
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                  id, project_id, experiment_id, backend, command, cwd,
                  expected_outputs_json, backend_hints_json, metadata_json,
                  status, progress_phase, progress_message, progress_updated_at,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job_recover_modal_submit",
                    project["id"],
                    exp["id"],
                    "modal",
                    "python train.py",
                    ".",
                    "[]",
                    '{"gpu":"H100","timeout":85000}',
                    "{}",
                    "submitting",
                    "starting",
                    "Starting execution",
                    now,
                    now,
                    now,
                ),
            )

        status = self.call(
            "job.status",
            project_id=project["id"],
            job_id="job_recover_modal_submit",
        )

        self.assertEqual(status["status"], "queued")
        self.assertEqual(backend.recovered[0]["job_id"], "job_recover_modal_submit")
        ui_status = self.app.jobs.get_status_for_ui(
            project_id=project["id"],
            job_id="job_recover_modal_submit",
            reconcile=False,
        )
        self.assertEqual(ui_status["runtime_job_id"], "modal:recovered")

    def test_backend_submit_errors_return_failed_job(self) -> None:
        class FailingBackend(FakeBackend):
            def submit(self, *, spec, progress=None):
                from backend.execution import BackendValidationError

                raise BackendValidationError("bad backend hints")

        app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "other.sqlite",
            execution_backend=FailingBackend(),
        )
        self.app = app
        project, exp = self.ready_experiment()
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
            backend_hints={"bad": True},
        )

        self.assertEqual(job["status"], "failed")
        # Error now prefixed with exception class name so opaque gRPC strings
        # (e.g., "No item with that key") aren't ambiguous about which Modal
        # call class raised. The message tail still carries the human text.
        self.assertEqual(job["error"], "BackendValidationError: bad backend hints")
        jobs = self.call("job.list", project_id=project["id"])["jobs"]
        self.assertEqual(jobs[0]["id"], job["id"])

    def test_materialization_retry_cap_surfaces_warning(self) -> None:
        class FailingMaterializeBackend(FakeBackend):
            def materialize_outputs(self, **kwargs):
                self.materialize_calls.append(kwargs["runtime_job_id"])
                raise RuntimeError("download failed")

        backend = FailingMaterializeBackend(materializes=True)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "retry.sqlite",
            execution_backend=backend,
        )
        project, exp = self.ready_experiment()
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
            expected_outputs=["results.json"],
        )
        runtime_id = backend.last_runtime_job_id
        backend.set_status(runtime_job_id=runtime_id, state="succeeded")

        with patch.dict("os.environ", {"RESEARCH_PLUGIN_MATERIALIZE_RETRIES": "3"}):
            status = {}
            for _ in range(4):
                status = self.call("job.status", project_id=project["id"], job_id=job["id"])

        self.assertEqual(len(backend.materialize_calls), 3)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["warning"], "download failed")
        ui_status = self.app.jobs.get_status_for_ui(project_id=project["id"], job_id=job["id"], reconcile=False)
        self.assertEqual(ui_status["materialize_attempts"], 3)
        self.assertEqual(ui_status["materialize_error"], "download failed")

    def test_materialization_does_not_hold_sqlite_write_transaction(self) -> None:
        class LockProbeBackend(FakeBackend):
            def __init__(self, *, db_path: Path) -> None:
                super().__init__(materializes=True)
                self.db_path = db_path
                self.acquired_write_lock = False

            def materialize_outputs(self, **kwargs):
                conn = sqlite3.connect(self.db_path, timeout=0.01)
                try:
                    conn.execute("PRAGMA busy_timeout = 1")
                    conn.execute("BEGIN IMMEDIATE")
                    conn.rollback()
                    self.acquired_write_lock = True
                finally:
                    conn.close()
                return super().materialize_outputs(**kwargs)

        db_path = self.repo / ".research_plugin" / "lock_probe.sqlite"
        backend = LockProbeBackend(db_path=db_path)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=db_path,
            execution_backend=backend,
        )
        project, exp = self.ready_experiment()
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python train.py",
            expected_outputs=["results.json"],
        )
        runtime_id = backend.last_runtime_job_id
        backend.set_status(runtime_job_id=runtime_id, state="succeeded")

        self.call("job.status", project_id=project["id"], job_id=job["id"])

        self.assertTrue(backend.acquired_write_lock)


class ComposeNestedStatusTests(unittest.TestCase):
    """Unit tests for the pure compose_nested_status function."""

    def test_terminal_states_drop_the_substate_suffix(self) -> None:
        from backend.services.jobs import compose_nested_status

        for terminal in ("succeeded", "failed", "cancelled"):
            self.assertEqual(
                compose_nested_status(status=terminal, progress_phase="anything"),
                terminal,
                f"terminal state {terminal} must not carry a substate suffix",
            )

    def test_progress_phase_appears_as_suffix_when_distinct_from_status(self) -> None:
        from backend.services.jobs import compose_nested_status

        self.assertEqual(
            compose_nested_status(status="queued", progress_phase="waiting_sandbox"),
            "queued.waiting_sandbox",
        )
        self.assertEqual(
            compose_nested_status(status="queued", progress_phase="runner_starting"),
            "queued.runner_starting",
        )
        self.assertEqual(
            compose_nested_status(status="running", progress_phase="materializing"),
            "running.materializing",
        )

    def test_redundant_phase_matching_status_is_collapsed(self) -> None:
        """If progress_phase mirrors status (legacy behavior pre-Phase 2), don't
        produce silly values like 'queued.queued'."""
        from backend.services.jobs import compose_nested_status

        self.assertEqual(
            compose_nested_status(status="queued", progress_phase="queued"),
            "queued",
        )

    def test_no_phase_returns_status_alone(self) -> None:
        from backend.services.jobs import compose_nested_status

        self.assertEqual(
            compose_nested_status(status="running", progress_phase=None),
            "running",
        )
        self.assertEqual(
            compose_nested_status(status="running", progress_phase=""),
            "running",
        )

    def test_live_pipeline_report_overrides_db_phase_for_submitting(self) -> None:
        """When a Modal submit is in flight, the pipeline's `current` is the
        freshest source — preferred over the DB progress_phase."""
        from backend.execution import SubmitStatusReport
        from backend.services.jobs import compose_nested_status

        live = SubmitStatusReport(
            stages=("preparing", "syncing", "acquiring_sandbox"),
            current="acquiring_sandbox",
            completed=("preparing", "syncing"),
        )
        self.assertEqual(
            compose_nested_status(
                status="submitting",
                progress_phase="syncing",  # stale DB
                live_report=live,
            ),
            "submitting.acquiring_sandbox",
        )

    def test_live_pipeline_with_no_current_falls_back_to_db_phase(self) -> None:
        """If the live pipeline hasn't entered any stage yet, fall through to
        the DB phase."""
        from backend.execution import SubmitStatusReport
        from backend.services.jobs import compose_nested_status

        empty_live = SubmitStatusReport(
            stages=("preparing", "syncing"),
            current=None,
            completed=(),
        )
        self.assertEqual(
            compose_nested_status(
                status="submitting",
                progress_phase="accepted",
                live_report=empty_live,
            ),
            "submitting.accepted",
        )


class JobServiceNestedStatusIntegrationTests(unittest.TestCase):
    """End-to-end: submit a job through the agent surface and confirm
    nested_status reaches the hydrated agent and UI dicts.

    Uses the FakeBackend (sync submit, no live pipeline), so the nested_status
    comes from the DB progress_phase. Modal's live-pipeline path is exercised
    in tests/test_submit_pipeline.py.

    Duplicates JobServiceTest.setUp / ready_experiment intentionally rather
    than inheriting — inheriting would re-run every JobServiceTest method
    inside this class.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def _ready_experiment(self):
        project = self.call("project.create", name="Nested status test")
        claim = self.call(
            "claim.create", project_id=project["id"], statement="Job output should exist."
        )
        exp = self.call(
            "experiment.create",
            project_id=project["id"],
            intent="Run a backend script.",
            tested_claim_ids=[claim["id"]],
        )
        (self.repo / "plan.md").write_text("metric: output exists\n")
        plan = self.call(
            "resource.register_file", project_id=project["id"], path="plan.md", kind="note"
        )
        self.call(
            "resource.associate",
            project_id=project["id"],
            resource_id=plan["id"],
            target_type="experiment",
            target_id=exp["id"],
            role="plan",
        )
        self.call(
            "experiment.transition",
            project_id=project["id"],
            experiment_id=exp["id"],
            transition="submit_design",
        )
        req = self.call(
            "review.request",
            project_id=project["id"],
            target_type="experiment",
            target_id=exp["id"],
            role="design_reviewer",
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id="design-reviewer",
        )
        self.call(
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict="pass",
            notes="ok",
        )
        self.call(
            "experiment.transition",
            project_id=project["id"],
            experiment_id=exp["id"],
            transition="mark_ready_to_run",
        )
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "train.py").write_text("print('training')\n")
        return project, exp

    def test_nested_status_appears_in_agent_and_ui_hydration(self) -> None:
        project, exp = self._ready_experiment()
        job = self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python scripts/train.py",
            expected_outputs=["experiments/e001/results.json"],
        )

        # Agent shape includes nested_status; FakeBackend's sync submit leaves
        # the job at status=queued with no extra phase.
        self.assertIn("nested_status", job)
        self.assertEqual(job["nested_status"], "queued")

        # UI shape (richer dict) also carries it.
        ui = self.app.jobs.get_status_for_ui(
            project_id=project["id"], job_id=job["id"], reconcile=False
        )
        self.assertEqual(ui["nested_status"], "queued")

        # Drive the backend to terminal and confirm the suffix drops.
        runtime_id = self.backend.last_runtime_job_id
        self.backend.set_status(runtime_job_id=runtime_id, state="succeeded")
        done = self.call("job.status", project_id=project["id"], job_id=job["id"])
        self.assertEqual(done["nested_status"], "succeeded")

    def test_job_list_summary_includes_nested_status(self) -> None:
        """job.list MCP tool returns summaries — they should also carry
        nested_status so agents listing jobs see the substate."""
        project, exp = self._ready_experiment()
        self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python scripts/train.py",
            expected_outputs=["experiments/e001/results.json"],
        )

        listing = self.call("job.list", project_id=project["id"])

        self.assertEqual(len(listing["jobs"]), 1)
        summary = listing["jobs"][0]
        self.assertIn("nested_status", summary)
        self.assertEqual(summary["nested_status"], "queued")

    def test_workflow_status_and_next_propagates_nested_status_into_jobs_list(self) -> None:
        """workflow.status_and_next embeds the project's jobs via
        jobs_for_experiment → _hydrate_for_ui. nested_status must reach the
        composite payload without any extra plumbing."""
        project, exp = self._ready_experiment()
        self.call(
            "job.submit",
            project_id=project["id"],
            experiment_id=exp["id"],
            command="python scripts/train.py",
            expected_outputs=["experiments/e001/results.json"],
        )

        status = self.call(
            "workflow.status_and_next",
            project_id=project["id"],
            experiment_id=exp["id"],
        )

        self.assertTrue(status["jobs"], "expected at least one job in the composite payload")
        self.assertIn("nested_status", status["jobs"][0])
        self.assertEqual(status["jobs"][0]["nested_status"], "queued")


if __name__ == "__main__":
    unittest.main()
