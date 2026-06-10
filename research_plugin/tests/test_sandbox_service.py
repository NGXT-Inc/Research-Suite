from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.http_api import ResearchHttpApi
from backend.execution.backends.fake import FakeSandboxBackend
from backend.execution.ssh_rsync import SshRsyncResult
from backend.execution.types import SandboxRequest
from backend.utils import NotFoundError, PermissionDeniedError, ValidationError


class FakeRsyncSyncer:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.push_calls: list[dict] = []

    def sync(self, **kwargs) -> SshRsyncResult:
        self.calls.append(dict(kwargs))
        return SshRsyncResult(
            pulled=2,
            duration_seconds=0.1,
            local_dir=str(kwargs["local_sync_dir"]),
            remote_dir=str(kwargs["remote_sync_dir"]),
            command_count=2,
            stdout="small.txt\n",
            stderr="",
        )

    def push_initial(self, **kwargs) -> SshRsyncResult:
        self.push_calls.append(dict(kwargs))
        return SshRsyncResult(
            pulled=1,
            duration_seconds=0.1,
            local_dir=str(kwargs["local_sync_dir"]),
            remote_dir=str(kwargs["remote_sync_dir"]),
            command_count=2,
            stdout="seed.txt\n",
            stderr="",
            direction="push",
        )


class SandboxServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.rsync = FakeRsyncSyncer()
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
            rsync_syncer=self.rsync,
        )
        self.project_id = self.call("project.create", name="Sandbox Project")["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _experiment(self, *, status: str = "ready_to_run") -> str:
        exp_id = self.call("experiment.create", project_id=self.project_id, intent="x")["id"]
        if status != "planned":
            with self.app.store.transaction() as conn:
                conn.execute("UPDATE experiments SET status = ? WHERE id = ?", (status, exp_id))
        return exp_id

    # ---- gating ----

    def test_request_requires_ready_or_running(self) -> None:
        exp_id = self._experiment(status="planned")
        with self.assertRaises(PermissionDeniedError):
            self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)

    def test_request_unknown_experiment(self) -> None:
        with self.assertRaises(NotFoundError):
            self.call("sandbox.request", project_id=self.project_id, experiment_id="exp_nope")

    # ---- procurement ----

    def test_request_creates_and_returns_ssh(self) -> None:
        exp_id = self._experiment()
        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id, gpu="A100", time_limit=1200
        )
        self.assertEqual(result["status"], "running")
        self.assertFalse(result["reused"])
        self.assertTrue(result["sandbox_id"])
        # Short agent-facing command goes through the repo-local dispatcher.
        self.assertEqual(result["ssh"]["command"], f".research_plugin/sbx {exp_id}")
        self.assertEqual(result["workdir"], "/workspace/synced")
        self.assertEqual(result["sync_dir"], "/workspace/synced")
        self.assertEqual(result["unsynced_dir"], "/workspace/unsynced")
        self.assertEqual(result["sandbox_data_dir"], "/workspace/unsynced")
        self.assertEqual(
            Path(result["local_sync_dir"]).resolve(),
            (self.repo / "experiments" / exp_id / "synced").resolve(),
        )
        self.assertEqual(self.rsync.push_calls[-1]["remote_sync_dir"], "/workspace/synced")
        self.assertEqual(Path(self.rsync.push_calls[-1]["local_sync_dir"]), Path(result["local_sync_dir"]))
        # Full ssh line is still available as a cwd-independent fallback.
        self.assertTrue(result["ssh"]["raw_command"].startswith("ssh -i "))
        self.assertIn("@sandbox.modal.test", result["ssh"]["raw_command"])
        self.assertTrue(Path(result["ssh"]["key_path"]).exists())
        self.assertTrue(Path(result["ssh"]["key_path"] + ".pub").exists())
        # experiment flips to running
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "running")

    def test_request_and_get_report_huggingface_env_without_secret_value(self) -> None:
        self.backend.sandbox_environment = lambda: {  # type: ignore[method-assign]
            "available_tokens": ["HF_TOKEN"],
            "notes": ["HF_TOKEN is available inside the sandbox."],
        }
        exp_id = self._experiment()
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["environment"]["available_tokens"], ["HF_TOKEN"])
        self.assertIn("Hugging Face", result["hint"])
        self.assertIn("HF_TOKEN", result["hint"])
        self.assertNotIn("hf_", str(result))

        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["environment"]["available_tokens"], ["HF_TOKEN"])
        self.assertIn("HF_TOKEN", got["hint"])

    def test_request_writes_dispatcher_and_conn(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        dispatcher = self.repo / ".research_plugin" / "sbx"
        conn = self.repo / ".research_plugin" / "sandboxes" / "conn" / exp_id
        self.assertTrue(dispatcher.exists())
        self.assertTrue(os.access(dispatcher, os.X_OK))
        self.assertTrue(conn.exists())
        body = conn.read_text()
        self.assertIn("RP_SSH_HOST=", body)
        self.assertIn("RP_SSH_PORT=", body)
        # Releasing the sandbox drops the conn file so `sbx` fails loudly.
        self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        self.assertFalse(conn.exists())

    def test_request_reuses_live_sandbox(self) -> None:
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        second = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertTrue(second["reused"])
        self.assertEqual(first["sandbox_id"], second["sandbox_id"])
        self.assertEqual(len(self.backend.acquired), 1)

    def test_request_recreates_after_death(self) -> None:
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.kill(sandbox_id=first["sandbox_id"])
        second = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertFalse(second["reused"])
        self.assertNotEqual(first["sandbox_id"], second["sandbox_id"])
        self.assertEqual(len(self.backend.acquired), 2)

    # ---- tunnel endpoint refresh (alive sandbox, moved tunnel) ----

    def test_get_refreshes_moved_endpoint(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        old_host = created["ssh"]["host"]
        # Sandbox stays alive but Modal relocates its SSH tunnel.
        self.backend.move_endpoint(
            sandbox_id=created["sandbox_id"], host="r999.modal.host", port=55555
        )
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "running")
        self.assertNotEqual(got["ssh"]["host"], old_host)
        self.assertEqual(got["ssh"]["host"], "r999.modal.host")
        self.assertEqual(got["ssh"]["port"], 55555)
        # The conn file the dispatcher sources must carry the refreshed endpoint.
        body = (self.repo / ".research_plugin" / "sandboxes" / "conn" / exp_id).read_text()
        self.assertIn("r999.modal.host", body)
        self.assertIn("55555", body)

    # ---- observability dashboards (MLflow + TensorBoard) ----

    def test_request_surfaces_dashboard_urls(self) -> None:
        # The agent view carries the dashboard URLs so the (rare) agent that
        # wants to show them in transcripts can; the user-facing UI view does
        # the same. URL strings come straight from the backend's encrypted
        # tunnel surface — no rewriting.
        exp_id = self._experiment()
        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertIn("dashboards", result)
        self.assertIn("mlflow", result["dashboards"])
        self.assertTrue(result["dashboards"]["mlflow"].startswith("https://mlflow-"))
        self.assertIn("tensorboard", result["dashboards"])
        # And the hint nudges the agent toward the auto-detect path so HF
        # Trainer and Lightning users get charts for free.
        self.assertIn("MLFLOW_TRACKING_URI", result["hint"])
        self.assertIn("mlflow.autolog", result["hint"])

    def test_ui_view_exposes_dashboards(self) -> None:
        # The HTTP API surfaces dashboards in the sandbox row so the UI can
        # render an iframe tab per non-empty entry. Empty {} when the backend
        # exposes none — never a missing key.
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        view = ResearchHttpApi(app=self.app).sandbox_get_view(
            project_id=self.project_id, experiment_id=exp_id
        )
        self.assertEqual(
            view["dashboards"],
            {
                "mlflow": f"https://mlflow-{created['sandbox_id']}.modal.test",
                "tensorboard": f"https://tensorboard-{created['sandbox_id']}.modal.test",
            },
        )

    def test_get_refreshes_moved_dashboards(self) -> None:
        # When Modal relocates a live sandbox's tunnels, the dashboard URLs
        # change alongside the SSH endpoint. Reconcile must persist the fresh
        # URLs so the UI iframe doesn't 404 on stale ones.
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        relocated = {
            "mlflow": "https://mlflow-r999.modal.host",
            "tensorboard": "https://tb-r999.modal.host",
        }
        self.backend.move_dashboards(
            sandbox_id=created["sandbox_id"], urls=relocated
        )
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["dashboards"], relocated)

    def test_dashboards_empty_when_backend_exposes_none(self) -> None:
        # CPU-only / older backends may surface no dashboards. The field must
        # still be present (empty dict) so the UI keys defensively.
        exp_id = self._experiment()
        # Pre-empty the backend's default before acquire stores the row.
        original_acquire = self.backend.acquire

        def acquire_without_dashboards(*, request, on_phase=None, on_created=None):
            provisioned = original_acquire(
                request=request, on_phase=on_phase, on_created=on_created
            )
            self.backend.dashboards[provisioned.sandbox_id] = {}
            from dataclasses import replace
            return replace(provisioned, dashboards={})

        self.backend.acquire = acquire_without_dashboards  # type: ignore[method-assign]
        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertEqual(result["dashboards"], {})
        view = ResearchHttpApi(app=self.app).sandbox_get_view(
            project_id=self.project_id, experiment_id=exp_id
        )
        self.assertEqual(view["dashboards"], {})

    # ---- status / liveness ----

    def test_get_reconciles_dead_sandbox(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.kill(sandbox_id=created["sandbox_id"])
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "terminated")

    def test_get_scoped_to_project(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        other = self.call("project.create", name="Other")["id"]
        with self.assertRaises(NotFoundError):
            self.call("sandbox.get", project_id=other, experiment_id=exp_id)

    # ---- live usage metrics ----

    def test_metrics_for_running_sandbox(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        sample = {
            "cpu": {"used_cores": 1.5, "limit_cores": 2.0},
            "memory": {"used_bytes": 2147483648, "limit_bytes": 8589934592},
            "gpus": [{"index": 0, "name": "A100", "util_pct": 42, "mem_used_mib": 1024, "mem_total_mib": 40960}],
        }
        self.backend.metrics[created["sandbox_id"]] = sample
        result = self.app.sandboxes.sample_metrics(
            project_id=self.project_id, experiment_id=exp_id
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["metrics"], sample)
        # The row's reserved request rides along to frame the bars.
        self.assertEqual(result["reserved"]["cpu"], 2.0)

    # ---- terminal ----

    def test_terminal_reads_transcript(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(experiment_id=exp_id, text="$ python train.py\nloss 0.1\n")
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertIn("train.py", term["transcript"])
        self.assertTrue(term["running"])
        self.assertEqual(term["cursor"], len(term["transcript"]))

    def test_terminal_since_returns_only_new_output(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(experiment_id=exp_id, text="epoch 1\n")
        first = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        cursor = first["cursor"]
        # No new output yet → since=cursor yields empty new output.
        same = self.call(
            "sandbox.terminal", project_id=self.project_id, experiment_id=exp_id, since=cursor
        )
        self.assertEqual(same["transcript"], "")
        self.assertEqual(same["new_chars"], 0)
        # New output appended → since=cursor returns ONLY the new bytes.
        self.backend.append_transcript(experiment_id=exp_id, text="epoch 2\n")
        delta = self.call(
            "sandbox.terminal", project_id=self.project_id, experiment_id=exp_id, since=cursor
        )
        self.assertEqual(delta["transcript"], "epoch 2\n")
        self.assertEqual(delta["new_chars"], len("epoch 2\n"))
        self.assertEqual(delta["cursor"], cursor + len("epoch 2\n"))

    def test_terminal_running_false_after_release(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertFalse(term["running"])

    def test_terminal_passes_stored_ssh_details_to_backend(self) -> None:
        # SSH-transcript backends (Lambda Labs) read the log over plain SSH, so
        # the registry must hand read_transcript the row's stored endpoint and
        # the per-experiment private key path.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        read = self.backend.transcript_reads[-1]
        self.assertEqual(read["ssh_host"], "sandbox.modal.test")
        self.assertEqual(read["ssh_port"], 40001)
        self.assertEqual(read["ssh_user"], "root")
        self.assertEqual(
            Path(read["key_path"]).resolve(),
            (self.repo / ".research_plugin" / "sandboxes" / "keys" / exp_id).resolve(),
        )

    # ---- terminal: per-command exit status (rec.sh markers) ----

    @staticmethod
    def _rec(command: str, output: str, exit_code: int, *, ts: str = "2026-06-09T12:00:00Z") -> str:
        """A completed-command transcript block in the rec.sh marker format."""
        return f"\n[{ts}] $ {command}\n{output}[{ts}] (exit {exit_code})\n"

    def test_terminal_parses_successful_exit_code(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id,
            text=self._rec("python train.py", "loss 0.1\n", 0, ts="2026-06-09T12:00:05Z"),
        )
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(term["last_exit_code"], 0)
        self.assertEqual(term["last_command_finished_at"], "2026-06-09T12:00:05Z")
        self.assertFalse(term["command_running"])

    def test_terminal_reports_nonzero_exit_code(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id, text=self._rec("false", "", 1)
        )
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(term["last_exit_code"], 1)
        self.assertFalse(term["command_running"])

    def test_terminal_command_running_when_no_exit_marker_yet(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        # A command started and is still streaming output — no exit marker yet.
        self.backend.append_transcript(
            experiment_id=exp_id, text="\n[2026-06-09T12:00:00Z] $ sleep 100\npartial...\n"
        )
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertTrue(term["command_running"])
        self.assertIsNone(term["last_exit_code"])
        self.assertIsNone(term["last_command_finished_at"])

    def test_terminal_uses_latest_exit_marker(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id, text=self._rec("true", "", 0, ts="2026-06-09T12:00:01Z")
        )
        self.backend.append_transcript(
            experiment_id=exp_id, text=self._rec("exit 2", "", 2, ts="2026-06-09T12:00:09Z")
        )
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(term["last_exit_code"], 2)
        self.assertEqual(term["last_command_finished_at"], "2026-06-09T12:00:09Z")
        self.assertFalse(term["command_running"])

    def test_terminal_exit_fields_null_without_markers(self) -> None:
        # Old-style transcript with no rec.sh markers → best-effort nulls.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(experiment_id=exp_id, text="$ python train.py\nloss 0.1\n")
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertIsNone(term["last_exit_code"])
        self.assertIsNone(term["last_command_finished_at"])
        self.assertFalse(term["command_running"])

    def test_terminal_exit_code_survives_since_cursor(self) -> None:
        # last_exit_code is parsed from the FULL transcript, so an incremental
        # poll that returns no new output still reports the finished command.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(experiment_id=exp_id, text=self._rec("true", "", 0))
        first = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        cursor = first["cursor"]
        delta = self.call(
            "sandbox.terminal", project_id=self.project_id, experiment_id=exp_id, since=cursor
        )
        self.assertEqual(delta["transcript"], "")
        self.assertEqual(delta["new_chars"], 0)
        self.assertEqual(delta["last_exit_code"], 0)

    def test_terminal_command_running_false_when_sandbox_dead(self) -> None:
        # A terminated sandbox whose log ends on a command-start marker is not
        # "running a command" — command_running is gated on the sandbox liveness.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id, text="\n[2026-06-09T12:00:00Z] $ sleep 100\n"
        )
        self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertFalse(term["running"])
        self.assertFalse(term["command_running"])

    def test_sync_commits_sandbox_and_returns_resource_guidance(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        result = self.call("sandbox.sync", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["sync"]["provider"], "ssh_rsync")
        self.assertEqual(result["sync"]["pulled"], 2)
        self.assertIn("resource.register_file", result["hint"])
        self.assertEqual(self.rsync.calls[-1]["ssh_host"], created["ssh"]["host"])
        self.assertEqual(self.rsync.calls[-1]["remote_sync_dir"], "/workspace/synced")

    def test_sync_requires_running_sandbox(self) -> None:
        exp_id = self._experiment()
        with self.assertRaises(ValidationError):
            self.call("sandbox.sync", project_id=self.project_id, experiment_id=exp_id)

    # ---- release ----

    def test_release_terminates(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        released = self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(released["status"], "terminated")
        self.assertIn(created["sandbox_id"], self.backend.terminated)

    # ---- list ----

    def test_list_returns_project_sandboxes(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        listed = self.call("sandbox.list", project_id=self.project_id)["sandboxes"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["experiment_id"], exp_id)

    # ---- validation ----

    def test_invalid_gpu_rejected(self) -> None:
        exp_id = self._experiment()
        with self.assertRaises(ValidationError):
            self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id, gpu="NOTREAL")

    def test_invalid_time_limit_rejected(self) -> None:
        exp_id = self._experiment()
        with self.assertRaises(ValidationError):
            self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id, time_limit=5)

    # ---- hardware selection (bundled-hardware backends like Lambda Labs) ----

    def _require_hardware_selection(self) -> None:
        """Flip the fake backend into Lambda-style bundled-hardware behavior."""
        from backend.execution.types import BackendCapabilities

        self.backend.capabilities = BackendCapabilities(
            name="fake",
            requires_hardware_selection=True,
            configurable_resources=False,
        )

        def catalog(*, gpu=None, region=None):
            options = [
                {"instance_type": "gpu_1x_a10", "gpu": "A10", "gpu_count": 1,
                 "vcpus": 30, "memory_gib": 200, "price_usd_per_hour": 0.75,
                 "regions": ["us-west-1"], "available": True},
                {"instance_type": "gpu_8x_h100", "gpu": "H100", "gpu_count": 8,
                 "vcpus": 208, "memory_gib": 1800, "price_usd_per_hour": 35.92,
                 "regions": ["us-east-1"], "available": True},
            ]
            if gpu:
                needle = str(gpu).upper()
                options = [o for o in options if needle in o["gpu"].upper()]
            return {
                "provider": "lambda_labs", "selection_required": True,
                "select_with": "instance_type", "reason": "bundled hardware",
                "regions": ["us-east-1", "us-west-1"], "count": len(options),
                "options": options,
            }

        self.backend.hardware_catalog = catalog  # type: ignore[attr-defined]

    def test_request_without_instance_type_returns_menu(self) -> None:
        self._require_hardware_selection()
        exp_id = self._experiment()
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "needs_selection")
        self.assertEqual(result["options"][0]["instance_type"], "gpu_1x_a10")
        self.assertIn("instance_type", result["hint"])
        # Nothing was provisioned — no money spent picking the menu.
        self.assertEqual(len(self.backend.acquired), 0)

    def test_request_with_instance_type_provisions_and_records_it(self) -> None:
        self._require_hardware_selection()
        exp_id = self._experiment()
        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id,
            instance_type="gpu_1x_a10", region="us-west-1",
        )
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["instance_type"], "gpu_1x_a10")
        self.assertEqual(result["region"], "us-west-1")
        self.assertEqual(len(self.backend.acquired), 1)
        self.assertEqual(self.backend.acquired[0].instance_type, "gpu_1x_a10")
        self.assertEqual(self.backend.acquired[0].region, "us-west-1")

    def test_request_freeform_gpu_not_rejected_on_bundled_backend(self) -> None:
        self._require_hardware_selection()
        exp_id = self._experiment()
        # 'A10' is not a Modal VALID_GPUS name; on a bundled backend it is a
        # filter, so it must not raise — it just still needs an instance_type.
        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id, gpu="A10"
        )
        self.assertEqual(result["status"], "needs_selection")

    def test_options_returns_backend_catalog(self) -> None:
        self._require_hardware_selection()
        result = self.call("sandbox.options", project_id=self.project_id)
        self.assertEqual(result["provider"], "lambda_labs")
        self.assertEqual(result["backend"], "fake")
        self.assertEqual(result["options"][0]["instance_type"], "gpu_1x_a10")
        self.assertIn("instance_type", result["hint"])

    def test_options_filters_by_gpu(self) -> None:
        self._require_hardware_selection()
        result = self.call("sandbox.options", project_id=self.project_id, gpu="h100")
        self.assertEqual([o["instance_type"] for o in result["options"]], ["gpu_8x_h100"])

    def test_reused_bundled_sandbox_skips_menu_and_keeps_instance_type(self) -> None:
        self._require_hardware_selection()
        exp_id = self._experiment()
        self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id,
            instance_type="gpu_1x_a10",
        )
        # A re-request without instance_type reuses the live sandbox rather than
        # re-prompting for selection.
        second = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertTrue(second["reused"])
        self.assertEqual(second["instance_type"], "gpu_1x_a10")

    def test_options_tool_is_registered(self) -> None:
        names = {tool["name"] for tool in self.app.list_tools()}
        self.assertIn("sandbox.options", names)

    # ---- expiration reaper ----

    def test_reaper_terminates_expired_sandbox(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        sid = created["sandbox_id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE experiment_id=?",
                ("2000-01-01T00:00:00Z", exp_id),
            )
        reaped = self.app.sandboxes.reap_expired()
        self.assertEqual(reaped, 1)
        self.assertIn(sid, self.backend.terminated)
        # A best-effort final sync runs before the kill so outputs survive.
        self.assertTrue(self.rsync.calls)
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "terminated")

    def test_reaper_reverts_running_experiment_to_ready(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "running")
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE experiment_id=?",
                ("2000-01-01T00:00:00Z", exp_id),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")

    def test_reaper_leaves_experiments_past_running_alone(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'experiment_review' WHERE id = ?", (exp_id,)
            )
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE experiment_id=?",
                ("2000-01-01T00:00:00Z", exp_id),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "experiment_review")

    def test_reaper_skips_unexpired_sandbox(self) -> None:
        exp_id = self._experiment()
        self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id, time_limit=3600
        )
        self.assertEqual(self.app.sandboxes.reap_expired(), 0)
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "running")

    # ---- async provisioning ----

    def _await_status(self, exp_id: str, target: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
            if got["status"] == target:
                return got
            time.sleep(0.02)
        return self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)

    def test_request_returns_provisioning_when_slow(self) -> None:
        # Budget below the gated acquire so request falls back to provisioning.
        self.app.sandboxes.request_wait_seconds = 0.05
        self.backend.gate = threading.Event()
        exp_id = self._experiment()
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "provisioning")
        self.assertEqual(result["poll_after_seconds"], 10)
        self.assertEqual(result["ssh"]["command"], "")
        # get keeps reporting provisioning while the job is gated.
        polled = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(polled["status"], "provisioning")
        # Release the gate; the job finishes and get flips to running with SSH.
        self.backend.gate.set()
        final = self._await_status(exp_id, "running")
        self.assertEqual(final["status"], "running")
        self.assertEqual(final["ssh"]["command"], f".research_plugin/sbx {exp_id}")

    def test_request_during_provisioning_does_not_double_provision(self) -> None:
        # The one-sandbox-per-experiment invariant: re-calling request while a
        # provision is in flight attaches to the SAME job (no second acquire),
        # and after it settles there is still exactly one sandbox.
        self.app.sandboxes.request_wait_seconds = 0.05
        self.backend.gate = threading.Event()
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(first["status"], "provisioning")
        second = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(second["status"], "provisioning")
        self.assertEqual(len(self.backend.acquired), 1)  # no duplicate provision
        self.backend.gate.set()
        final = self._await_status(exp_id, "running")
        self.assertEqual(final["status"], "running")
        self.assertEqual(len(self.backend.acquired), 1)

    def test_provisioning_failure_marks_failed_and_cleans_up(self) -> None:
        self.app.sandboxes.request_wait_seconds = 2.0
        self.backend.fail_after_create = True
        exp_id = self._experiment()
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["error"])
        # The sandbox that was created before the tunnel failure got terminated.
        self.assertTrue(self.backend.terminated)

    def test_release_cancels_provisioning(self) -> None:
        self.app.sandboxes.request_wait_seconds = 0.05
        self.backend.gate = threading.Event()
        exp_id = self._experiment()
        started = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(started["status"], "provisioning")
        self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        # Let the gated job unwind; it must honor the cancel, not go running.
        self.backend.gate.set()
        final = self._await_status(exp_id, "terminated")
        self.assertEqual(final["status"], "terminated")

    def test_get_reconciles_orphaned_provisioning(self) -> None:
        # A provisioning row with no in-flight job (daemon restart mid-provision)
        # must reconcile to failed so a polling agent doesn't wait forever.
        exp_id = self._experiment()
        self.app.sandboxes._begin_provisioning_row(
            experiment_id=exp_id,
            project_id=self.project_id,
            req=SandboxRequest(experiment_id=exp_id, project_id=self.project_id, public_key="k"),
        )
        result = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "failed")

    def test_get_returns_none_when_never_requested(self) -> None:
        exp_id = self._experiment()
        result = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "none")


if __name__ == "__main__":
    unittest.main()
