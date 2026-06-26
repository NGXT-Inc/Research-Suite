from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.app import ResearchPluginApp
from backend.transport.http_api import ResearchHttpApi
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.mlflow_tracking import CentralMlflowService
from backend.sandbox.sandbox_backend import SandboxRequest
from backend.utils import NotFoundError, PermissionDeniedError, ValidationError
from tests.fakes import FakeProcess, FakeRsyncSyncer, write_fake_mlflow_db


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
            mlflow_tracking=CentralMlflowService(
                mode="external",
                tracking_uri="https://mlflow.test",
                health_check=lambda: True,
            ),
        )
        self.project_id = self.call("project.create", name="Sandbox Project")["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _experiment(self, *, status: str = "ready_to_run", name: str = "exp-1") -> str:
        exp_id = self.call("experiment.create", name=name, project_id=self.project_id, intent="x")["id"]
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
        self.assertEqual(result["workdir"], "/workspace/exp-1")
        self.assertEqual(result["experiment_dir"], "/workspace/exp-1")
        self.assertEqual(result["data_dir"], "/workspace/data")
        self.assertEqual(
            Path(result["local_experiment_dir"]).resolve(),
            (self.repo / "experiments" / "exp-1").resolve(),
        )
        # The agent is told the folder contract at the moment it matters.
        self.assertIn("experiment folder", result["hint"])
        self.assertIn("OUTSIDE the experiment folder", result["hint"])
        self.assertIn("the sandbox owns the folder", result["hint"])
        self.assertIn("Call sandbox.sync", result["hint"])
        self.assertIn("expires at", result["hint"])
        self.assertIn("ready_to_run", result["hint"])
        # Full ssh line is still available as a cwd-independent fallback.
        self.assertTrue(result["ssh"]["raw_command"].startswith("ssh -i "))
        self.assertIn("@sandbox.modal.test", result["ssh"]["raw_command"])
        self.assertTrue(Path(result["ssh"]["key_path"]).exists())
        self.assertTrue(Path(result["ssh"]["key_path"] + ".pub").exists())
        # experiment flips to running
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "running")
        tracking_env = self.backend.acquired[-1].tracking_env
        self.assertEqual(tracking_env["MLFLOW_TRACKING_URI"], "https://mlflow.test")
        self.assertEqual(
            tracking_env["MLFLOW_EXPERIMENT_NAME"], f"rp/{self.project_id}/{exp_id}"
        )

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

    def test_attach_repoints_live_sandbox_to_another_experiment(self) -> None:
        source = self._experiment(name="exp-1")
        target = self._experiment(name="exp-2")
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=source
        )
        uid = created["sandbox_uid"]
        old_key = self.app.sandboxes.mgmt_keys.key_path(sandbox_uid=uid)
        self.assertTrue(old_key.exists())

        attached = self.call(
            "sandbox.attach",
            project_id=self.project_id,
            experiment_id=target,
            sandbox_uid=uid,
        )

        self.assertEqual(attached["sandbox_uid"], uid)
        self.assertEqual(attached["sandbox_id"], created["sandbox_id"])
        self.assertEqual(attached["experiment_id"], target)
        self.assertEqual(attached["status"], "running")
        self.assertTrue(attached["reused"])
        self.assertEqual(attached["source_experiment_id"], source)
        self.assertTrue(attached["source_experiment_reverted"])
        self.assertEqual(attached["workdir"], "/workspace/exp-2")
        self.assertEqual(attached["ssh"]["command"], f".research_plugin/sbx {target}")

        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=uid)
        self.assertEqual(row["experiment_id"], target)
        self.assertEqual(row["mgmt_key_ref"], uid)
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=source)["status"],
            "ready_to_run",
        )
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=target)["status"],
            "running",
        )
        conn = self.app.store.connect()
        try:
            attachments = conn.execute(
                """
                SELECT experiment_id, detached_at
                FROM sandbox_attachments
                WHERE sandbox_uid = ?
                ORDER BY experiment_id
                """,
                (uid,),
            ).fetchall()
        finally:
            conn.close()
        by_experiment = {row["experiment_id"]: row["detached_at"] for row in attachments}
        self.assertEqual(set(by_experiment), {source, target})
        self.assertIsNotNone(by_experiment[source])
        self.assertIsNone(by_experiment[target])
        conn_dir = self.repo / ".research_plugin" / "sandboxes" / "conn"
        self.assertFalse((conn_dir / source).exists())
        self.assertTrue((conn_dir / target).exists())
        self.assertTrue((conn_dir / uid).exists())
        self.assertEqual(self.backend.retarget_calls[-1]["experiment_id"], target)
        self.assertEqual(self.backend.retarget_calls[-1]["workdir"], "/workspace/exp-2")
        self.assertEqual(self.backend.retarget_calls[-1]["key_path"], str(old_key))

        self.call("sandbox.terminal", project_id=self.project_id, experiment_id=target)
        self.assertEqual(self.backend.transcript_reads[-1]["key_path"], str(old_key))

    def test_attach_refreshes_source_alias_when_sibling_remains(self) -> None:
        source = self._experiment(name="exp-1")
        target = self._experiment(name="exp-2")
        primary = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=source
        )
        sibling = self.call(
            "sandbox.request",
            project_id=self.project_id,
            experiment_id=source,
            additional=True,
        )

        attached = self.call(
            "sandbox.attach",
            project_id=self.project_id,
            experiment_id=target,
            sandbox_uid=primary["sandbox_uid"],
        )

        self.assertFalse(attached["source_experiment_reverted"])
        self.assertEqual(
            self.call(
                "experiment.get_state",
                project_id=self.project_id,
                experiment_id=source,
            )["status"],
            "running",
        )
        self.assertEqual(
            self.call("sandbox.get", project_id=self.project_id, experiment_id=source)[
                "sandbox_uid"
            ],
            sibling["sandbox_uid"],
        )
        conn = self.repo / ".research_plugin" / "sandboxes" / "conn" / source
        self.assertTrue(conn.exists())
        self.assertIn(f"RP_SSH_PORT='{40002}'", conn.read_text())

    def test_attach_rejects_dead_sandbox_and_non_runnable_target(self) -> None:
        source = self._experiment(name="exp-1")
        planned = self._experiment(status="planned", name="exp-2")
        runnable = self._experiment(name="exp-3")
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=source
        )
        with self.assertRaises(PermissionDeniedError):
            self.call(
                "sandbox.attach",
                project_id=self.project_id,
                experiment_id=planned,
                sandbox_uid=created["sandbox_uid"],
            )

        self.backend.kill(sandbox_id=created["sandbox_id"])
        with self.assertRaises(ValidationError):
            self.call(
                "sandbox.attach",
                project_id=self.project_id,
                experiment_id=runnable,
                sandbox_uid=created["sandbox_uid"],
            )

    def test_request_additional_creates_parallel_sandbox(self) -> None:
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        second = self.call(
            "sandbox.request",
            project_id=self.project_id,
            experiment_id=exp_id,
            additional=True,
        )

        self.assertNotEqual(first["sandbox_uid"], second["sandbox_uid"])
        self.assertNotEqual(first["sandbox_id"], second["sandbox_id"])
        self.assertEqual(first["ssh"]["command"], f".research_plugin/sbx {exp_id}")
        self.assertEqual(
            second["ssh"]["command"],
            f".research_plugin/sbx {second['sandbox_uid']}",
        )
        self.assertEqual(first["workdir"], "/workspace/exp-1")
        self.assertNotEqual(first["workdir"], second["workdir"])
        self.assertIn(second["sandbox_uid"][:12], second["workdir"])

        conn_dir = self.repo / ".research_plugin" / "sandboxes" / "conn"
        self.assertTrue((conn_dir / first["sandbox_uid"]).exists())
        self.assertTrue((conn_dir / second["sandbox_uid"]).exists())
        rows = self.app.sandboxes.registry.list_by_experiment(experiment_id=exp_id)
        self.assertEqual({row["sandbox_uid"] for row in rows}, {first["sandbox_uid"], second["sandbox_uid"]})
        listed = self.call("sandbox.list", project_id=self.project_id)["sandboxes"]
        self.assertIn(first["sandbox_uid"], {row["sandbox_uid"] for row in listed})
        self.assertIn(second["sandbox_uid"], {row["sandbox_uid"] for row in listed})
        # The back-compat experiment-keyed read targets the most recent live row.
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["sandbox_uid"], second["sandbox_uid"])

    def test_get_sync_terminal_and_release_target_sandbox_uid(self) -> None:
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        second = self.call(
            "sandbox.request",
            project_id=self.project_id,
            experiment_id=exp_id,
            additional=True,
        )

        got_first = self.call(
            "sandbox.get",
            project_id=self.project_id,
            experiment_id=exp_id,
            sandbox_uid=first["sandbox_uid"],
        )
        self.assertEqual(got_first["sandbox_id"], first["sandbox_id"])
        synced = self.call(
            "sandbox.sync",
            project_id=self.project_id,
            experiment_id=exp_id,
            sandbox_uid=first["sandbox_uid"],
        )
        self.assertEqual(synced["sandbox_uid"], first["sandbox_uid"])
        self.assertEqual(self.rsync.calls[-1]["remote_sync_dir"], first["workdir"])

        self.call(
            "sandbox.terminal",
            project_id=self.project_id,
            experiment_id=exp_id,
            sandbox_uid=second["sandbox_uid"],
        )
        self.assertEqual(self.backend.transcript_reads[-1]["sandbox_id"], second["sandbox_id"])

        released = self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            sandbox_uid=first["sandbox_uid"],
        )
        self.assertEqual(released["sandbox_uid"], first["sandbox_uid"])
        self.assertIn(first["sandbox_id"], self.backend.terminated)
        self.assertTrue(self.backend.is_alive(sandbox_id=second["sandbox_id"]))
        primary = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(primary["sandbox_uid"], second["sandbox_uid"])

    def test_release_provisioning_sibling_does_not_touch_live_primary(self) -> None:
        exp_id = self._experiment()
        primary = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        pending_uid = self.app.sandboxes.registry.create_sandbox(
            experiment_id=exp_id,
            project_id=self.project_id,
            status="provisioning",
            workdir="/workspace/exp-1-pending",
            sync_dir="/workspace/exp-1-pending",
        )

        released = self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            sandbox_uid=pending_uid,
        )

        self.assertEqual(released["sandbox_uid"], pending_uid)
        self.assertTrue(self.backend.is_alive(sandbox_id=primary["sandbox_id"]))
        self.assertNotIn(primary["sandbox_id"], self.backend.terminated)
        conn = self.app.store.connect()
        try:
            gens = conn.execute(
                "SELECT ended_at FROM sandbox_generations WHERE experiment_id = ?",
                (exp_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(gens), 1)
        self.assertIsNone(gens[0]["ended_at"])

    def test_release_without_uid_releases_all_live_sandboxes(self) -> None:
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        second = self.call(
            "sandbox.request",
            project_id=self.project_id,
            experiment_id=exp_id,
            additional=True,
        )

        released = self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(released["released_count"], 2)
        self.assertIn(first["sandbox_id"], self.backend.terminated)
        self.assertIn(second["sandbox_id"], self.backend.terminated)
        statuses = {
            row["sandbox_uid"]: row["status"]
            for row in self.app.sandboxes.registry.list_by_experiment(experiment_id=exp_id)
        }
        self.assertEqual(statuses[first["sandbox_uid"]], "terminated")
        self.assertEqual(statuses[second["sandbox_uid"]], "terminated")

    def test_reaper_reverts_experiment_only_after_last_live_sandbox(self) -> None:
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        second = self.call(
            "sandbox.request",
            project_id=self.project_id,
            experiment_id=exp_id,
            additional=True,
        )
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE sandbox_uid=?",
                ("2000-01-01T00:00:00Z", first["sandbox_uid"]),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "running")

        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE sandbox_uid=?",
                ("2000-01-01T00:00:00Z", second["sandbox_uid"]),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")

    def test_reaper_keeps_experiment_running_while_sibling_provisions(self) -> None:
        # Regression (F1): a still-provisioning sibling must keep the experiment
        # alive when the running sandbox reaps — has_active must count provisioning.
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        second = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id, additional=True
        )
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET status='provisioning' WHERE sandbox_uid=?",
                (second["sandbox_uid"],),
            )
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE sandbox_uid=?",
                ("2000-01-01T00:00:00Z", first["sandbox_uid"]),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "running")

    def test_additional_sandbox_gets_a_distinct_local_dir(self) -> None:
        # Regression (F2): parallel sandboxes must NOT share one local sync dir, or
        # a uid-targeted sync (rsync --delete) would clobber the primary's folder.
        exp_id = self._experiment()
        primary = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        extra = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id, additional=True
        )
        self.assertNotEqual(
            primary["local_experiment_dir"], extra["local_experiment_dir"]
        )
        self.assertTrue(
            extra["local_experiment_dir"].endswith(extra["sandbox_uid"][:12])
        )

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

    # ---- observability dashboards (central MLflow + TensorBoard) ----

    def test_tunnel_ready_waits_out_the_ssh_handshake(self) -> None:
        # The ssh -L listener binds only after the handshake (0.5-2s); an
        # instant probe always loses that race. _tunnel_ready must wait for
        # the bind, then probe end-to-end.
        import http.server
        from backend.dataplane.sandbox_dashboards import _free_local_port, _tunnel_ready

        port = _free_local_port()
        server: list = []

        def serve_after_delay() -> None:
            time.sleep(0.7)  # simulate the ssh handshake window
            handler = type(
                "OK",
                (http.server.BaseHTTPRequestHandler,),
                {
                    "do_GET": lambda self: (self.send_response(200), self.end_headers()),
                    "log_message": lambda self, *a: None,
                },
            )
            srv = http.server.HTTPServer(("127.0.0.1", port), handler)
            server.append(srv)
            srv.serve_forever()

        thread = threading.Thread(target=serve_after_delay, daemon=True)
        thread.start()
        fake_ssh = subprocess.Popen(["sleep", "30"])
        try:
            self.assertTrue(
                _tunnel_ready(fake_ssh, port, f"http://127.0.0.1:{port}", timeout=6.0)
            )
        finally:
            fake_ssh.kill()
            if server:
                server[0].shutdown()

    def test_tunnel_ready_fails_fast_when_ssh_dies(self) -> None:
        from backend.dataplane.sandbox_dashboards import _free_local_port, _tunnel_ready

        port = _free_local_port()
        dead_ssh = subprocess.Popen(["true"])
        dead_ssh.wait()
        self.assertFalse(
            _tunnel_ready(dead_ssh, port, f"http://127.0.0.1:{port}", timeout=2.0)
        )

    def test_tunnel_ready_times_out_without_listener(self) -> None:
        from backend.dataplane.sandbox_dashboards import _free_local_port, _tunnel_ready

        port = _free_local_port()
        fake_ssh = subprocess.Popen(["sleep", "30"])
        try:
            start = time.monotonic()
            self.assertFalse(
                _tunnel_ready(fake_ssh, port, f"http://127.0.0.1:{port}", timeout=0.8)
            )
            self.assertLess(time.monotonic() - start, 3.0)
        finally:
            fake_ssh.kill()

    def test_request_surfaces_tensorboard_and_central_mlflow_context(self) -> None:
        # TensorBoard remains a dashboard URL. MLflow is backend-owned tracking
        # context that agents must apply to quantitative training commands.
        exp_id = self._experiment()
        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertIn("dashboards", result)
        self.assertIn("tensorboard", result["dashboards"])
        self.assertNotIn("mlflow", result["dashboards"])
        self.assertEqual(result["mlflow"]["tracking_uri"], "https://mlflow.test")
        self.assertEqual(
            result["mlflow"]["experiment_name"], f"rp/{self.project_id}/{exp_id}"
        )
        self.assertEqual(
            result["mlflow"]["env"]["MLFLOW_TRACKING_URI"], "https://mlflow.test"
        )
        self.assertEqual(
            result["mlflow"]["env"]["MLFLOW_EXPERIMENT_NAME"],
            f"rp/{self.project_id}/{exp_id}",
        )
        self.assertIn("MLFLOW_TRACKING_URI", result["hint"])
        self.assertIn("centralized MLflow", result["hint"])
        self.assertIn("$RP_TB_LOGDIR", result["hint"])
        self.assertIn("params, metrics, and artifacts", result["hint"])
        self.assertIn("mlflow.autolog", result["hint"])
        self.assertIn("figures/*.png", result["hint"])
        self.assertIn("report.md", result["hint"])
        self.assertIn("Call sandbox.sync to pull", result["hint"])

    def test_local_loopback_mlflow_starts_reverse_tunnel_for_remote_sandbox(self) -> None:
        self.app.sandboxes.mlflow_tracking = CentralMlflowService(
            mode="managed",
            tracking_uri="http://127.0.0.1:5678",
            server_uri="http://127.0.0.1:5678",
            dashboard_url="http://127.0.0.1:5678",
            health_check=lambda: True,
        )
        self.app.sandboxes.metrics.mlflow_tracking = self.app.sandboxes.mlflow_tracking
        process = MagicMock()
        process.poll.return_value = None
        exp_id = self._experiment()
        self.app.worker.ensure_keypair(experiment_id=exp_id)
        real_popen = subprocess.Popen

        def mlflow_popen(command, **kwargs):
            command = list(command)
            # Mgmt-key minting (ssh-keygen) runs for real; the patch stands in
            # only for the mlflow reverse tunnel.
            if command and command[0] == "ssh-keygen":
                return real_popen(command, **kwargs)
            return process

        with (
            patch(
                "backend.dataplane.mlflow_tunnels.subprocess.Popen",
                side_effect=mlflow_popen,
            ) as popen,
            patch("backend.dataplane.mlflow_tunnels._forward_bound", return_value=True),
        ):
            result = self.call(
                "sandbox.request", project_id=self.project_id, experiment_id=exp_id
            )

        command = popen.call_args.args[0]
        self.assertIn("-R", command)
        self.assertIn("127.0.0.1:5678:127.0.0.1:5678", command)
        self.assertEqual(result["mlflow"]["access"]["ready"], True)
        self.assertEqual(
            result["mlflow"]["access"]["tracking_uri"], "http://127.0.0.1:5678"
        )
        self.assertIn("MLFLOW_TRACKING_URI", result["hint"])

    def test_mlflow_reverse_tunnel_ensure_is_single_flight(self) -> None:
        from backend.dataplane.mlflow_tunnels import MlflowReverseTunnels

        process = MagicMock()
        process.poll.return_value = None
        row = {
            "project_id": self.project_id,
            "experiment_id": "exp_single",
            "sandbox_id": "sbx_single",
            "status": "running",
            "ssh_host": "sandbox.modal.test",
            "ssh_port": 22,
            "ssh_user": "root",
        }
        manager = MlflowReverseTunnels(key_path=lambda **_: self.repo / "key")
        barrier = threading.Barrier(6)
        results: list[dict] = []

        def ensure() -> None:
            barrier.wait()
            results.append(
                manager.ensure(row=row, tracking_uri="http://127.0.0.1:5678")
            )

        def ready(_process) -> bool:
            time.sleep(0.02)
            return True

        threads = [threading.Thread(target=ensure) for _ in range(5)]
        with (
            patch(
                "backend.dataplane.mlflow_tunnels.subprocess.Popen",
                return_value=process,
            ) as popen,
            patch(
                "backend.dataplane.mlflow_tunnels._forward_bound",
                side_effect=ready,
            ),
        ):
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=1.0)
        try:
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(len(results), 5)
            self.assertTrue(all(result["ready"] for result in results))
        finally:
            manager.stop()

    def test_shutdown_stops_mlflow_tunnels(self) -> None:
        with patch.object(self.app.sandboxes.worker, "stop_mlflow_access") as stop:
            self.app.sandboxes.shutdown()
        stop.assert_called_once_with()

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
            {"tensorboard": f"https://tensorboard-{created['sandbox_id']}.modal.test"},
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

    # ---- durable metrics archive (results outlive the VM) ----

    SNAPSHOT = {
        "source": "mlflow",
        "base_url": "https://mlflow-x.modal.test",
        "experiments": [
            {
                "experiment_id": "1",
                "name": "lora_glue",
                "last_update_time": 99,
                "runs": [
                    {
                        "run_id": "r1",
                        "run_name": "seed_0",
                        "status": "FINISHED",
                        "params": {"lr": "0.0005"},
                        "metrics": {"acc": {"last": 0.91}},
                        "history": {"acc": [[10, 0.85], [20, 0.91]]},
                    }
                ],
            }
        ],
    }

    def test_release_archives_metrics_before_terminating(self) -> None:
        # Central MLflow is durable, but release still captures a final snapshot
        # so the UI has a compact metrics record after termination.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        with patch(
            "backend.services.sandbox.sandbox_metrics.snapshot_mlflow",
            return_value=dict(self.SNAPSHOT),
        ) as snap:
            self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        snap.assert_called_once()
        self.assertEqual(snap.call_args.args[0], "https://mlflow.test")
        self.assertEqual(
            snap.call_args.kwargs["experiment_name"], f"rp/{self.project_id}/{exp_id}"
        )
        result = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["sandbox_status"], "terminated")
        self.assertNotIn("base_url", result)
        self.assertEqual(result["experiments"][0]["name"], "lora_glue")
        self.assertEqual(
            result["experiments"][0]["runs"][0]["metrics"]["acc"]["last"], 0.91
        )
        self.assertIn("captured_at", result)
        with self.app.store.transaction() as conn:
            rows = conn.execute(
                "SELECT type FROM events WHERE type = 'sandbox.metrics_persisted'"
            ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_results_metrics_lazy_central_snapshot_strips_base_url(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        with patch(
            "backend.services.sandbox.sandbox_metrics.snapshot_mlflow",
            return_value=dict(self.SNAPSHOT),
        ):
            result = self.app.sandboxes.results_metrics(
                experiment_id=exp_id, project_id=self.project_id
            )

        self.assertTrue(result["available"])
        self.assertNotIn("base_url", result)
        self.assertEqual(result["experiments"][0]["name"], "lora_glue")

    def test_central_metrics_override_legacy_snapshot(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        row = self.app.sandboxes.registry.fetch_scoped(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.app.sandboxes.mlflow_tracking.server_uri = "http://mlflow:5000"
        legacy = {"source": "mlflow", "experiments": [{"name": "legacy_local"}]}
        with patch(
            "backend.services.sandbox.sandbox_metrics.snapshot_mlflow",
            return_value=dict(self.SNAPSHOT),
        ) as snap:
            self.app.sandboxes.metrics.persist_row(
                row=row, force=True, snapshot=legacy, snapshot_provided=True
            )
        self.assertEqual(snap.call_args.args[0], "http://mlflow:5000")
        result = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertEqual(result["experiments"][0]["name"], "lora_glue")

    def test_explicit_sync_archives_metrics(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        with patch(
            "backend.services.sandbox.sandbox_metrics.snapshot_mlflow",
            return_value=dict(self.SNAPSHOT),
        ):
            self.call("sandbox.sync", project_id=self.project_id, experiment_id=exp_id)
        result = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["sandbox_status"], "running")

    def test_unreachable_mlflow_keeps_last_good_archive(self) -> None:
        # A temporarily unreachable central server at release time must not wipe
        # the archive captured during earlier syncs: None means "skip", never
        # "overwrite with empty".
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        with patch(
            "backend.services.sandbox.sandbox_metrics.snapshot_mlflow",
            return_value=dict(self.SNAPSHOT),
        ):
            self.call("sandbox.sync", project_id=self.project_id, experiment_id=exp_id)
        with patch("backend.services.sandbox.sandbox_metrics.snapshot_mlflow", return_value=None):
            self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)
        result = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["experiments"][0]["name"], "lora_glue")

    def test_metrics_survive_without_the_daemon_files(self) -> None:
        # Plan Phase 5: snapshots are control-plane records. Wipe every
        # daemon-side metrics file — the read path still serves them, so
        # reviews/UI see metrics without the user machine online.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        with patch(
            "backend.services.sandbox.sandbox_metrics.snapshot_mlflow",
            return_value=dict(self.SNAPSHOT),
        ):
            self.call("sandbox.sync", project_id=self.project_id, experiment_id=exp_id)
        archive_path = self.app.sandboxes.metrics_archive.path_for(exp_id)
        self.assertTrue(archive_path.exists())  # the local file cache is kept as-is
        archive_path.unlink()
        result = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["experiments"][0]["name"], "lora_glue")
        self.assertIn("captured_at", result)
        with self.app.store.transaction() as conn:
            record = conn.execute(
                "SELECT project_id, source FROM metrics_snapshots WHERE experiment_id = ?",
                (exp_id,),
            ).fetchone()
        self.assertIsNotNone(record)
        self.assertEqual(record["project_id"], self.project_id)
        self.assertEqual(record["source"], "mlflow")

    def test_pre_record_archive_converges_into_the_control_record(self) -> None:
        # A pre-Phase-5 experiment has only the daemon file cache: the first
        # read serves it AND lands it as a control record.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.app.sandboxes.metrics_archive.persist(
            experiment_id=exp_id, snapshot=dict(self.SNAPSHOT)
        )
        self.assertIsNone(
            self.app.sandboxes.metrics_records.load(experiment_id=exp_id)
        )
        result = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertTrue(result["available"])
        record = self.app.sandboxes.metrics_records.load(experiment_id=exp_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["experiments"][0]["name"], "lora_glue")

    def test_results_metrics_unavailable_shape(self) -> None:
        exp_id = self._experiment()
        result = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["sandbox_status"], "none")
        self.assertIn("hint", result)

    def test_results_metrics_backfills_from_pulled_mlflow_db(self) -> None:
        # Rescue path: a sandbox terminated before REST archiving ever ran,
        # but the rsync pull captured its mlflow.db. The first read extracts
        # the archive from that file and persists it.
        exp_id = self._experiment()
        db_path = self.app.sandboxes.worker.pulled_mlflow_db_path(experiment_id=exp_id, name=self.app.sandboxes.registry.experiment_name(experiment_id=exp_id))
        write_fake_mlflow_db(db_path)
        result = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["experiments"][0]["name"], "lora_glue")
        self.assertEqual(
            result["experiments"][0]["runs"][0]["metrics"]["acc"]["last"], 0.91
        )
        # Persisted: a second read works even if the pulled file disappears.
        db_path.unlink()
        again = self.app.sandboxes.results_metrics(
            experiment_id=exp_id, project_id=self.project_id
        )
        self.assertTrue(again["available"])

    def test_local_dashboard_tunnels_when_backend_has_ports_but_no_urls(self) -> None:
        self.backend.default_dashboards = False
        self.backend.local_dashboard_ports = lambda: {"tensorboard": 6006}  # type: ignore[attr-defined]
        exp_id = self._experiment()
        # Mint both keypairs before patching Popen: sandbox_dashboards shares
        # the global subprocess module, so the patch would otherwise swallow
        # the ssh-keygen runs inside sandbox.request.
        self.app.sandboxes._ensure_keypair(experiment_id=exp_id)
        popen_calls: list[list[str]] = []
        procs: list[tuple[list[str], FakeProcess]] = []
        real_popen = subprocess.Popen

        def fake_popen(command, **kwargs):
            command = list(command)
            # Mgmt-key minting (ssh-keygen) must run for real; the patch only
            # stands in for the ssh dashboard tunnels.
            if command and command[0] == "ssh-keygen":
                return real_popen(command, **kwargs)
            popen_calls.append(command)
            proc = FakeProcess()
            procs.append((command, proc))
            return proc

        class FakeResponse:
            status_code = 200

        with (
            patch("backend.dataplane.sandbox_dashboards.subprocess.Popen", side_effect=fake_popen),
            # The bind-wait would loop against a port nobody listens on; a
            # connectable socket stands in for ssh having bound the forward.
            patch("backend.dataplane.sandbox_dashboards.socket.create_connection", return_value=MagicMock()),
            patch("backend.dataplane.sandbox_dashboards.httpx.get", return_value=FakeResponse()),
        ):
            result = self.call(
                "sandbox.request", project_id=self.project_id, experiment_id=exp_id
            )
            dashboards = result["dashboards"]
            self.assertEqual(set(dashboards), {"tensorboard"})
            self.assertTrue(dashboards["tensorboard"].startswith("http://127.0.0.1:"))
            self.assertEqual(len(popen_calls), 1)
            self.assertTrue(
                any(
                    any(part.endswith(":127.0.0.1:6006") for part in command)
                    for command in popen_calls
                )
            )
            self.call("sandbox.release", project_id=self.project_id, experiment_id=exp_id)

        ssh_procs = [proc for command, proc in procs if command and command[0] == "ssh"]
        self.assertTrue(ssh_procs)
        self.assertTrue(all(proc.terminated for proc in ssh_procs))

    # ---- status / liveness ----

    def test_get_reconciles_dead_sandbox(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.kill(sandbox_id=created["sandbox_id"])
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "terminated")

    def test_get_reconcile_reverts_experiment_when_sandbox_dies(self) -> None:
        # A sandbox that dies underneath a running experiment (provider stopped
        # it, e.g. a billing/idle stop) is detected by reconcile on the next
        # sandbox.get. The experiment must not be stranded in 'running' — it
        # reverts to ready_to_run so the agent can request a fresh sandbox,
        # exactly as the expiry reaper does.
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)["status"],
            "running",
        )
        self.backend.kill(sandbox_id=created["sandbox_id"])
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "terminated")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")

    def test_get_reconcile_keeps_experiment_running_while_sibling_alive(self) -> None:
        # With a parallel (additional) sandbox still live, reconciling one dead
        # sandbox must keep the experiment running; only the last live sandbox
        # dying reverts it.
        exp_id = self._experiment()
        first = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        second = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id, additional=True
        )
        self.backend.kill(sandbox_id=first["sandbox_id"])
        self.call(
            "sandbox.get",
            project_id=self.project_id,
            experiment_id=exp_id,
            sandbox_uid=first["sandbox_uid"],
        )
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)["status"],
            "running",
        )
        self.backend.kill(sandbox_id=second["sandbox_id"])
        self.call(
            "sandbox.get",
            project_id=self.project_id,
            experiment_id=exp_id,
            sandbox_uid=second["sandbox_uid"],
        )
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)["status"],
            "ready_to_run",
        )

    def test_get_scoped_to_project(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        other = self.call("project.create", name="Other")["id"]
        with self.assertRaises(NotFoundError):
            self.call("sandbox.get", project_id=other, experiment_id=exp_id)

    def test_get_scoped_to_tenant(self) -> None:
        project_id = self.app.projects.create(
            name="Tenant Sandbox", tenant_id="tenant_a"
        )["id"]
        sandbox_uid = "uid_tenant"
        self.app.sandboxes.registry.upsert(
            experiment_id="exp_tenant",
            sandbox_uid=sandbox_uid,
            project_id=project_id,
            status="failed",
            sandbox_id="sbx_tenant",
            workdir="/workspace/experiments/tenant",
            sync_dir="/workspace/experiments/tenant",
        )

        with self.assertRaises(NotFoundError):
            self.app.sandboxes.get(
                experiment_id="exp_tenant",
                sandbox_uid=sandbox_uid,
                project_id=project_id,
                tenant_id="tenant_b",
                include_data_plane_enrichment=False,
            )
        with self.assertRaises(ValidationError):
            self.app.sandboxes.get(
                experiment_id="exp_tenant",
                sandbox_uid=sandbox_uid,
                tenant_id="tenant_b",
                include_data_plane_enrichment=False,
            )

        got = self.app.sandboxes.get(
            experiment_id="exp_tenant",
            sandbox_uid=sandbox_uid,
            project_id=project_id,
            tenant_id="tenant_a",
            include_data_plane_enrichment=False,
        )
        self.assertEqual(got["experiment_id"], "exp_tenant")
        self.assertEqual(got["sandbox_id"], "sbx_tenant")

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

    def test_terminal_authenticates_with_the_management_key(self) -> None:
        # SSH-transcript backends (Lambda Labs) read the log over SSH; the
        # registry hands read_transcript the row's stored endpoint and the
        # per-sandbox MANAGEMENT key (plan Phase 5, fixed decision 4) — never
        # the user key, which stays data-plane-only.
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        read = self.backend.transcript_reads[-1]
        self.assertEqual(read["ssh_host"], "sandbox.modal.test")
        self.assertEqual(read["ssh_port"], 40001)
        self.assertEqual(read["ssh_user"], "root")
        self.assertEqual(
            Path(read["key_path"]).resolve(),
            (
                self.repo
                / ".research_plugin"
                / "mgmt_keys"
                / row["sandbox_uid"]
                / "key"
            ).resolve(),
        )
        user_key = self.repo / ".research_plugin" / "sandboxes" / "keys" / exp_id
        self.assertNotEqual(Path(read["key_path"]).resolve(), user_key.resolve())

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
        self.assertEqual(self.rsync.calls[-1]["remote_sync_dir"], "/workspace/exp-1")

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
        self.assertIn("final pull", released["hint"])
        self.assertIn("metrics snapshot", released["hint"])
        self.assertIn("sandbox.sync", released["hint"])
        self.assertNotIn("parachute", released["hint"].lower())
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
        from backend.sandbox.sandbox_backend import BackendCapabilities

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
        self.assertEqual(result["poll_after_seconds"], 30)
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
        self.app.sandboxes.provisioner.begin_provisioning_row(
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
