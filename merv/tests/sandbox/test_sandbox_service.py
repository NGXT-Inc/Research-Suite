from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from tests.support.brain import TestBrain
from backend.execution.backends.fake import FakeSandboxBackend
from backend.mlflow import CentralMlflowService
from backend.sandbox.sandbox_backend import (
    BackendCapabilities,
    SandboxRequest,
)
from backend.ports.quota_admission import AdmissionRequest
from backend.services.sandbox.sandbox_provisioner import SandboxProvisioner
from backend.utils import NotFoundError, PermissionDeniedError, ValidationError, parse_iso
from backend.workspace import local_experiment_dir


class SandboxServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
            mlflow_tracking=CentralMlflowService(
                mode="external",
                tracking_uri="https://mlflow.test",
                health_check=lambda: True,
            ),
        )
        self.project_id = self.call("project", action="create", name="Sandbox Project")["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def _record_running_command(self, *, sandbox_uid: str) -> None:
        self.app.sandboxes.registry.record_command_snapshot(
            sandbox_uid=sandbox_uid,
            snapshot={
                "command_id": "cmd_running",
                "command": "python train.py",
                "started_at": "2026-06-09T12:00:00Z",
                "status": "running",
                "exit_code": None,
                "finished_at": None,
                "output_tail": "epoch 1",
            },
        )

    def _experiment(self, *, status: str = "ready_to_run", name: str = "exp-1") -> str:
        exp_id = self.call("experiment.create", name=name, project_id=self.project_id, intent="x")["id"]
        if status != "planned":
            with self.app.store.transaction() as conn:
                conn.execute("UPDATE experiments SET status = ? WHERE id = ?", (status, exp_id))
        return exp_id

    # ---- gating ----

    def test_request_allows_planned_experiment_attachment(self) -> None:
        exp_id = self._experiment(status="planned")
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["active_experiment_ids"], [exp_id])
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "planned")

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
        uid = result["sandbox_uid"]
        # The unified brain returns provider facts only. Local command/key
        # enrichment belongs to the stdio proxy data plane.
        self.assertIn("host", result["ssh"])
        self.assertNotIn("command", result["ssh"])
        self.assertEqual(result["workdir"], f"/workspace/sandbox-{uid[:12]}")
        self.assertEqual(result["experiment_dir"], f"/workspace/sandbox-{uid[:12]}")
        self.assertEqual(result["data_dir"], "/workspace/data")
        self.assertEqual(result["public_key_source"], "caller")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")

    def test_request_without_experiment_creates_standalone_sandbox(self) -> None:
        result = self.call("sandbox.request", project_id=self.project_id)

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["experiment_id"], "")
        self.assertTrue(result["sandbox_uid"])

    def test_cpu_sandbox_records_zero_gpu_count(self) -> None:
        result = self.call("sandbox.request", project_id=self.project_id)
        conn = self.app.store.connect()
        try:
            row = conn.execute(
                "SELECT gpu_count FROM sandbox_generations WHERE sandbox_id = ?",
                (result["sandbox_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(row["gpu_count"]), 0)
        self.assertNotIn("command", result["ssh"])
        self.assertEqual(result["public_key_source"], "caller")
        self.assertEqual(self.backend.acquired[-1].experiment_id, result["sandbox_uid"])

        got = self.call(
            "sandbox.get",
            project_id=self.project_id,
            sandbox_uid=result["sandbox_uid"],
        )
        self.assertEqual(got["sandbox_id"], result["sandbox_id"])
        other_project = self.call("project", action="create", name="Other Project")["id"]
        with self.assertRaises(NotFoundError):
            self.call(
                "sandbox.get",
                project_id=other_project,
                sandbox_uid=result["sandbox_uid"],
            )
        terminal = self.call(
            "sandbox.terminal",
            project_id=self.project_id,
            sandbox_uid=result["sandbox_uid"],
        )
        self.assertEqual(terminal["status"], "running")

        released = self.call(
            "sandbox.release",
            project_id=self.project_id,
            sandbox_uid=result["sandbox_uid"],
            confirm_retained=True,
        )
        self.assertEqual(released["status"], "terminated")
        self.assertEqual(self.backend.terminated, [result["sandbox_id"]])

    def test_request_and_get_report_huggingface_env_without_secret_value(self) -> None:
        self.backend.sandbox_environment = lambda: {  # type: ignore[method-assign]
            "available_tokens": ["HF_TOKEN"],
            "notes": ["HF_TOKEN is available inside the sandbox."],
        }
        exp_id = self._experiment()
        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertEqual(result["environment"]["available_tokens"], ["HF_TOKEN"])
        self.assertNotIn("hf_", str(result))

        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["environment"]["available_tokens"], ["HF_TOKEN"])

    def test_secret_delivery_retries_until_write_succeeds(self) -> None:
        attempts: list[str] = []
        self.backend.sandbox_secrets = lambda: {"HF_TOKEN": "secret"}  # type: ignore[method-assign]

        def write_secrets(**kwargs) -> bool:
            attempts.append(kwargs["sandbox_id"])
            return len(attempts) > 1

        self.backend.write_secrets = write_secrets  # type: ignore[method-assign]
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )

        self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)

        self.assertEqual(attempts, [created["sandbox_id"], created["sandbox_id"]])

    def test_no_configured_secrets_is_terminal(self) -> None:
        reads = 0

        def sandbox_secrets() -> dict[str, str]:
            nonlocal reads
            reads += 1
            return {}

        self.backend.sandbox_secrets = sandbox_secrets  # type: ignore[method-assign]
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)

        self.assertEqual(reads, 1)

    def test_request_reuses_live_sandbox(self) -> None:
        exp_id = self._experiment()
        first = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        second = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertTrue(second["reused"])
        self.assertEqual(first["sandbox_id"], second["sandbox_id"])
        self.assertEqual(len(self.backend.acquired), 1)

    def test_attach_associates_live_sandbox_with_another_experiment(self) -> None:
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
        self.assertEqual(set(attached["active_experiment_ids"]), {source, target})
        self.assertEqual(attached["status"], "running")
        self.assertTrue(attached["reused"])
        self.assertEqual(attached["workdir"], created["workdir"])
        self.assertNotIn("command", attached["ssh"])

        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=uid)
        self.assertEqual(row["mgmt_key_ref"], uid)
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=source)["status"],
            "ready_to_run",
        )
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=target)["status"],
            "ready_to_run",
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
        self.assertIsNone(by_experiment[source])
        self.assertIsNone(by_experiment[target])
        self.call("sandbox.terminal", project_id=self.project_id, experiment_id=target)
        self.assertEqual(self.backend.transcript_reads[-1]["key_path"], str(old_key))

    def test_attach_standalone_sandbox_to_experiment(self) -> None:
        target = self._experiment(name="exp-2")
        created = self.call("sandbox.request", project_id=self.project_id)
        uid = created["sandbox_uid"]

        attached = self.call(
            "sandbox.attach",
            project_id=self.project_id,
            experiment_id=target,
            sandbox_uid=uid,
        )

        self.assertEqual(attached["sandbox_uid"], uid)
        self.assertEqual(attached["experiment_id"], target)
        self.assertEqual(attached["active_experiment_ids"], [target])
        self.assertNotIn("command", attached["ssh"])
        self.assertEqual(
            self.call(
                "experiment.get_state",
                project_id=self.project_id,
                experiment_id=target,
            )["status"],
            "ready_to_run",
        )
        conn = self.app.store.connect()
        try:
            attachments = conn.execute(
                """
                SELECT experiment_id, detached_at
                FROM sandbox_attachments
                WHERE sandbox_uid = ?
                """,
                (uid,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [(row["experiment_id"], row["detached_at"]) for row in attachments],
            [(target, None)],
        )

    def test_attach_preserves_source_association_when_sibling_remains(self) -> None:
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

        self.assertEqual(set(attached["active_experiment_ids"]), {source, target})
        self.assertEqual(
            self.call(
                "experiment.get_state",
                project_id=self.project_id,
                experiment_id=source,
            )["status"],
            "ready_to_run",
        )
        self.assertEqual(
            self.call("sandbox.get", project_id=self.project_id, experiment_id=source)[
                "sandbox_uid"
            ],
            sibling["sandbox_uid"],
        )
        self.assertNotEqual(primary["sandbox_uid"], sibling["sandbox_uid"])

    def test_attach_allows_planned_target_but_rejects_dead_sandbox(self) -> None:
        source = self._experiment(name="exp-1")
        planned = self._experiment(status="planned", name="exp-2")
        runnable = self._experiment(name="exp-3")
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=source
        )
        attached = self.call(
            "sandbox.attach",
            project_id=self.project_id,
            experiment_id=planned,
            sandbox_uid=created["sandbox_uid"],
        )
        self.assertIn(planned, attached["active_experiment_ids"])

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
        self.assertNotIn("command", first["ssh"])
        self.assertNotIn("command", second["ssh"])
        self.assertEqual(first["workdir"], f"/workspace/sandbox-{first['sandbox_uid'][:12]}")
        self.assertNotEqual(first["workdir"], second["workdir"])
        self.assertIn(second["sandbox_uid"][:12], second["workdir"])

        rows = self.app.sandboxes.registry.list_by_experiment(experiment_id=exp_id)
        self.assertEqual({row["sandbox_uid"] for row in rows}, {first["sandbox_uid"], second["sandbox_uid"]})
        listed = self.call("sandbox.list", project_id=self.project_id)["sandboxes"]
        self.assertIn(first["sandbox_uid"], {row["sandbox_uid"] for row in listed})
        self.assertIn(second["sandbox_uid"], {row["sandbox_uid"] for row in listed})
        # The back-compat experiment-keyed read targets the most recent live row.
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["sandbox_uid"], second["sandbox_uid"])

    def test_get_terminal_and_release_target_sandbox_uid(self) -> None:
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
            confirm_retained=True,
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
            confirm_retained=True,
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

        released = self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        self.assertEqual(released["released_count"], 2)
        self.assertIn(first["sandbox_id"], self.backend.terminated)
        self.assertIn(second["sandbox_id"], self.backend.terminated)
        rows = self.app.sandboxes.registry.list_by_experiment(experiment_id=exp_id)
        self.assertEqual(rows, [])

    def test_reaper_does_not_change_experiment_status(self) -> None:
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
        self.assertEqual(state["status"], "ready_to_run")

        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE sandboxes SET expires_at=? WHERE sandbox_uid=?",
                ("2000-01-01T00:00:00Z", second["sandbox_uid"]),
            )
        self.assertEqual(self.app.sandboxes.reap_expired(), 1)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")

    def test_reaper_keeps_provisioning_sibling_association(self) -> None:
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
        self.assertEqual(state["status"], "ready_to_run")

    def test_additional_sandbox_gets_a_distinct_local_dir(self) -> None:
        # Regression (F2): parallel sandboxes must NOT share one local experiment
        # folder, or explicit retained-file copies would collide.
        exp_id = self._experiment()
        primary = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        extra = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id, additional=True
        )
        primary_dir = local_experiment_dir(
            repo_root=self.repo,
            experiment_id=primary["sandbox_uid"],
            name=f"sandbox-{primary['sandbox_uid'][:12]}",
        )
        extra_dir = local_experiment_dir(
            repo_root=self.repo,
            experiment_id=extra["sandbox_uid"],
            name=f"sandbox-{extra['sandbox_uid'][:12]}",
        )
        self.assertNotEqual(primary_dir, extra_dir)
        self.assertTrue(str(extra_dir).endswith(extra["sandbox_uid"][:12]))

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
        task_type, payload = self.app.sandboxes.tasks.history[-1]
        self.assertEqual(task_type, "conn_refresh")
        self.assertEqual(payload["row"]["ssh_host"], "r999.modal.host")
        self.assertEqual(payload["row"]["ssh_port"], 55555)

    # ---- sandbox response guidance ----

    def test_request_has_no_sandbox_dashboard_or_mlflow_context(self) -> None:
        exp_id = self._experiment()
        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertNotIn("dashboards", result)
        self.assertNotIn("mlflow", result)
        self.assertNotIn("hint", result)

    # ---- status / liveness ----

    def test_get_reconciles_dead_sandbox(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.kill(sandbox_id=created["sandbox_id"])
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "terminated")

    def test_get_reconcile_does_not_change_experiment_when_sandbox_dies(self) -> None:
        # A sandbox that dies underneath an associated experiment is detected by
        # reconcile on the next sandbox.get. The sandbox terminates and its active
        # attachment closes; the experiment status is not a sandbox concern.
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(
            self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)["status"],
            "ready_to_run",
        )
        self.backend.kill(sandbox_id=created["sandbox_id"])
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "terminated")
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")

    def test_get_reconcile_keeps_sibling_attachment_alive(self) -> None:
        # With a parallel (additional) sandbox still live, reconciling one dead
        # sandbox removes only that sandbox's attachment.
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
            "ready_to_run",
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
        other = self.call("project", action="create", name="Other")["id"]
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

    def test_terminal_since_survives_transcripts_beyond_the_tail_window(self) -> None:
        # Regression: backends only return a ~50KB tail window. The cursor used
        # to be computed from the window length, so once the log passed 50KB it
        # pinned there and since= polls returned "" forever. Backends now report
        # the transcript's TRUE byte size, so the cursor keeps advancing and an
        # incremental poll at the previous cursor gets exactly the new bytes.
        window_len = 50_000
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        big = ("x" * 99 + "\n") * 600  # 60,000 bytes — beyond the tail window
        self.backend.append_transcript(experiment_id=exp_id, text=big)

        first = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(first["cursor"], len(big))  # true size, not window size
        self.assertEqual(len(first["transcript"]), window_len)
        self.assertTrue(big.endswith(first["transcript"]))

        self.backend.append_transcript(experiment_id=exp_id, text="epoch 2\n")
        delta = self.call(
            "sandbox.terminal",
            project_id=self.project_id,
            experiment_id=exp_id,
            since=first["cursor"],
        )
        self.assertEqual(delta["transcript"], "epoch 2\n")
        self.assertEqual(delta["new_chars"], len("epoch 2\n"))
        self.assertEqual(delta["cursor"], len(big) + len("epoch 2\n"))

        # A cursor that has slid out of the window clamps to the window start:
        # the poller gets the whole window (newest bytes at the right offsets)
        # instead of a slice at a meaningless in-window offset.
        stale = self.call(
            "sandbox.terminal",
            project_id=self.project_id,
            experiment_id=exp_id,
            since=1_000,
        )
        self.assertEqual(len(stale["transcript"]), window_len)
        self.assertTrue(stale["transcript"].endswith("epoch 2\n"))
        self.assertEqual(stale["cursor"], len(big) + len("epoch 2\n"))

    def test_terminal_running_false_after_release(self) -> None:
        exp_id = self._experiment()
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        term = self.call(
            "sandbox.terminal",
            project_id=self.project_id,
            sandbox_uid=result["sandbox_uid"],
        )
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
        self.assertFalse(term["command_status_stale"])
        self.assertEqual(term["last_command"]["command"], "python train.py")
        self.assertEqual(term["last_command"]["started_at"], "2026-06-09T12:00:05Z")
        self.assertEqual(term["last_command"]["status"], "succeeded")
        self.assertEqual(term["last_command"]["exit_code"], 0)
        self.assertIn("loss 0.1", term["last_command"]["output_tail"])
        self.assertTrue(term["last_command"]["command_id"].startswith("cmd_"))

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

    def test_terminal_keeps_last_finished_exit_while_next_command_runs(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id,
            text=(
                self._rec("true", "", 0, ts="2026-06-09T12:00:01Z")
                + "\n[2026-06-09T12:00:10Z] $ sleep 100\npartial...\n"
            ),
        )
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        self.assertTrue(term["command_running"])
        self.assertEqual(term["last_exit_code"], 0)
        self.assertEqual(term["last_command_finished_at"], "2026-06-09T12:00:01Z")
        self.assertEqual(term["last_command"]["command"], "sleep 100")
        self.assertEqual(term["last_command"]["status"], "running")

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
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id, text="\n[2026-06-09T12:00:00Z] $ sleep 100\n"
        )
        self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        term = self.call(
            "sandbox.terminal",
            project_id=self.project_id,
            sandbox_uid=result["sandbox_uid"],
        )
        self.assertFalse(term["running"])
        self.assertFalse(term["command_running"])

    def test_terminal_returns_stale_command_status_when_read_unavailable(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id,
            text=self._rec("python train.py", "loss 0.1\n", 0, ts="2026-06-09T12:00:05Z"),
        )
        first = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        cursor = first["cursor"]

        def unavailable(**_kwargs):
            raise RuntimeError("ssh unavailable")

        self.backend.read_transcript = unavailable  # type: ignore[method-assign]
        term = self.call(
            "sandbox.terminal",
            project_id=self.project_id,
            experiment_id=exp_id,
            since=cursor,
        )

        self.assertIn("terminal unavailable", term["transcript"])
        self.assertTrue(term["command_status_stale"])
        self.assertEqual(term["last_exit_code"], 0)
        self.assertEqual(term["last_command_finished_at"], "2026-06-09T12:00:05Z")
        self.assertFalse(term["command_running"])
        self.assertEqual(term["last_command"]["command"], "python train.py")
        self.assertEqual(term["last_command"]["status"], "succeeded")
        self.assertEqual(term["last_command"]["exit_code"], 0)

    def test_repeated_terminal_reads_do_not_rewrite_the_snapshot(self) -> None:
        # The UI polls terminal every few seconds; an unchanged snapshot must
        # not touch the canonical row (updated_at would become meaningless).
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id,
            text=self._rec("python train.py", "loss 0.1\n", 0, ts="2026-06-09T12:00:05Z"),
        )
        self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        with self.app.store.transaction() as conn:
            first = conn.execute("SELECT updated_at FROM sandboxes").fetchone()["updated_at"]
        for _ in range(3):
            self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        with self.app.store.transaction() as conn:
            row = conn.execute("SELECT updated_at FROM sandboxes").fetchone()
        self.assertEqual(row["updated_at"], first)

    def test_older_transcript_reader_cannot_regress_a_finished_snapshot(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.backend.append_transcript(
            experiment_id=exp_id,
            text=self._rec("python train.py", "loss 0.1\n", 0, ts="2026-06-09T12:00:05Z"),
        )
        term = self.call("sandbox.terminal", project_id=self.project_id, experiment_id=exp_id)
        finished = term["last_command"]
        self.assertEqual(finished["status"], "succeeded")
        with self.app.store.transaction() as conn:
            uid = conn.execute("SELECT sandbox_uid FROM sandboxes").fetchone()["sandbox_uid"]
        # A slow reader that parsed an older transcript (same command, no exit
        # marker yet) must not overwrite the finished snapshot.
        stale = {
            "command_id": finished["command_id"],
            "command": finished["command"],
            "started_at": finished["started_at"],
            "status": "running",
            "exit_code": None,
            "finished_at": None,
            "output_tail": "loss 0.1",
        }
        result = self.app.sandboxes.registry.record_command_snapshot(
            sandbox_uid=uid, snapshot=stale
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)

    # ---- release ----

    def test_release_terminates(self) -> None:
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        released = self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        self.assertEqual(released["status"], "terminated")
        self.assertIn("Only files the agent explicitly copied or uploaded", released["hint"])
        self.assertIn(created["sandbox_id"], self.backend.terminated)

    def test_release_requires_retention_confirmation(self) -> None:
        # Two-step release: the first call WITHOUT confirm_retained must NOT
        # terminate — it returns a retention checklist and leaves the sandbox
        # alive. Only confirm_retained=True actually destroys the VM.
        exp_id = self._experiment()
        created = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        pending = self.call(
            "sandbox.release", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertEqual(pending["status"], "confirmation_required")
        self.assertFalse(pending["released"])
        self.assertTrue(pending["pending_release"])
        self.assertNotIn(created["sandbox_id"], self.backend.terminated)
        self.assertTrue(self.backend.is_alive(sandbox_id=created["sandbox_id"]))
        # The sandbox is still running and visible.
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "running")
        # Confirming actually terminates it.
        released = self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        self.assertEqual(released["status"], "terminated")
        self.assertIn(created["sandbox_id"], self.backend.terminated)

    # ---- list ----

    def test_list_returns_project_sandboxes(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        listed = self.call("sandbox.list", project_id=self.project_id)["sandboxes"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["experiment_id"], exp_id)

    # ---- lifetime extension ----

    def test_extend_adds_one_default_thirty_minute_increment(self) -> None:
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request",
            project_id=self.project_id,
            experiment_id=exp_id,
            time_limit=1200,
        )
        old_expiry = parse_iso(created["expires_at"])
        self._record_running_command(sandbox_uid=created["sandbox_uid"])

        extended = self.call(
            "sandbox.extend",
            project_id=self.project_id,
            experiment_id=exp_id,
        )

        self.assertTrue(extended["extended"])
        self.assertEqual(extended["extended_by_seconds"], 1800)
        self.assertEqual(extended["time_limit"], 3000)
        self.assertEqual(
            int((parse_iso(extended["expires_at"]) - old_expiry).total_seconds()),
            1800,
        )
        events = self.app.store.recent_events(project_id=self.project_id)["events"]
        self.assertTrue(
            any(event["type"] == "sandbox.lifetime_extended" for event in events)
        )

    def test_extend_can_target_sandbox_uid_with_smaller_increment(self) -> None:
        created = self.call("sandbox.request", project_id=self.project_id)
        self.app.sandboxes.registry.record_heartbeat(
            experiment_id="",
            sandbox_uid=created["sandbox_uid"],
            idle_since=None,
            snapshot={
                "sampled_at": "2026-06-09T12:00:30Z",
                "metrics": {"cpu": {"used_cores": 0.30}},
            },
        )

        extended = self.call(
            "sandbox.extend",
            project_id=self.project_id,
            sandbox_uid=created["sandbox_uid"],
            seconds=600,
        )

        self.assertEqual(extended["sandbox_uid"], created["sandbox_uid"])
        self.assertEqual(extended["extended_by_seconds"], 600)
        self.assertEqual(extended["time_limit"], 4200)

    def test_extend_budget_denial_does_not_change_expiry(self) -> None:
        created = self.call(
            "sandbox.request", project_id=self.project_id, gpu="H100", time_limit=3600
        )
        self._record_running_command(sandbox_uid=created["sandbox_uid"])
        self.app.quotas.set_quota(tenant_id="local", gpu_hours_budget=1.1)

        with self.assertRaises(PermissionDeniedError):
            self.call(
                "sandbox.extend",
                project_id=self.project_id,
                sandbox_uid=created["sandbox_uid"],
                seconds=1800,
            )

        row = self.app.sandboxes.registry.get_by_uid(
            sandbox_uid=created["sandbox_uid"]
        )
        self.assertEqual(row["expires_at"], created["expires_at"])

    def test_extend_rejects_idle_sandbox(self) -> None:
        created = self.call("sandbox.request", project_id=self.project_id)

        with self.assertRaisesRegex(ValidationError, "active heartbeat"):
            self.call(
                "sandbox.extend",
                project_id=self.project_id,
                sandbox_uid=created["sandbox_uid"],
            )

    def test_extend_rejects_provider_without_lifetime_extension(self) -> None:
        exp_id = self._experiment()
        created = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )
        self.backend.capabilities = BackendCapabilities(name="modal")

        with self.assertRaisesRegex(ValidationError, "do not support"):
            self.call(
                "sandbox.extend",
                project_id=self.project_id,
                sandbox_uid=created["sandbox_uid"],
            )

    def test_extend_rejects_past_max_total_lifetime(self) -> None:
        exp_id = self._experiment()
        self.call(
            "sandbox.request",
            project_id=self.project_id,
            experiment_id=exp_id,
            time_limit=86400,
        )

        with self.assertRaisesRegex(ValidationError, "max lifetime"):
            self.call("sandbox.extend", project_id=self.project_id, experiment_id=exp_id)

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
                """
                UPDATE sandboxes
                SET expires_at=?
                WHERE sandbox_uid IN (
                  SELECT sandbox_uid FROM sandbox_attachments WHERE experiment_id=?
                )
                """,
                ("2000-01-01T00:00:00Z", exp_id),
            )
        reaped = self.app.sandboxes.reap_expired()
        self.assertEqual(reaped, 1)
        self.assertIn(sid, self.backend.terminated)
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(got["status"], "none")

    def test_reaper_does_not_change_running_experiment(self) -> None:
        exp_id = self._experiment()
        self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        state = self.call("experiment.get_state", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(state["status"], "ready_to_run")
        with self.app.store.transaction() as conn:
            conn.execute(
                """
                UPDATE sandboxes
                SET expires_at=?
                WHERE sandbox_uid IN (
                  SELECT sandbox_uid FROM sandbox_attachments WHERE experiment_id=?
                )
                """,
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
                """
                UPDATE sandboxes
                SET expires_at=?
                WHERE sandbox_uid IN (
                  SELECT sandbox_uid FROM sandbox_attachments WHERE experiment_id=?
                )
                """,
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

    def _await_sandbox_status(
        self, sandbox_uid: str, target: str, timeout: float = 5.0
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = self.call(
                "sandbox.get", project_id=self.project_id, sandbox_uid=sandbox_uid
            )
            if got["status"] == target:
                return got
            time.sleep(0.02)
        return self.call(
            "sandbox.get", project_id=self.project_id, sandbox_uid=sandbox_uid
        )

    def test_request_returns_provisioning_when_slow(self) -> None:
        # Budget below the gated acquire so request falls back to provisioning.
        self.app.sandboxes.request_wait_seconds = 0.05
        self.backend.gate = threading.Event()
        exp_id = self._experiment()
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "provisioning")
        self.assertEqual(result["poll_after_seconds"], 30)
        self.assertNotIn("command", result["ssh"])
        # get keeps reporting provisioning while the job is gated.
        polled = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(polled["status"], "provisioning")
        # Release the gate; the job finishes and get flips to running with SSH.
        self.backend.gate.set()
        final = self._await_status(exp_id, "running")
        self.assertEqual(final["status"], "running")
        self.assertEqual(final["ssh"]["host"], "sandbox.modal.test")

    def test_request_during_provisioning_does_not_double_provision(self) -> None:
        # Re-calling request while a provision is in flight reuses the same job
        # unless the caller explicitly asks for an additional sandbox.
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

    def test_sandbox_uid_retry_without_experiment_reuses_claim(self) -> None:
        self.app.sandboxes.request_wait_seconds = 0.05
        self.backend.gate = threading.Event()
        first = self.call("sandbox.request", project_id=self.project_id)
        second = self.call(
            "sandbox.request",
            project_id=self.project_id,
            sandbox_uid=first["sandbox_uid"],
        )
        self.assertEqual(second["sandbox_uid"], first["sandbox_uid"])
        self.assertEqual(second["status"], "provisioning")
        self.assertEqual(len(self.backend.acquired), 1)
        self.backend.gate.set()

    def test_db_claim_precedes_cross_process_orphan_cleanup(self) -> None:
        uid = "uid_cross_process"
        entered = threading.Event()
        release = threading.Event()

        def lookup(**_kwargs):
            entered.set()
            release.wait(timeout=2)
            return None

        self.backend.find_sandbox_id = lookup  # type: ignore[method-assign]
        provisioners = [
            SandboxProvisioner(
                registry=self.app.sandboxes.registry,
                backend=self.backend,
                lifecycle=self.app.sandboxes.lifecycle,
                quotas=self.app.quotas,
                stale_provision_seconds=600,
            )
            for _ in range(2)
        ]
        req = SandboxRequest(
            experiment_id=uid,
            project_id=self.project_id,
            public_key="k",
            sandbox_uid=uid,
        )
        admission = AdmissionRequest(
            tenant_id="local",
            time_limit_seconds=req.time_limit,
            gpu_count=0,
            sandbox_uid=uid,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                provisioners[0].ensure_job,
                experiment_id="",
                project_id=self.project_id,
                req=req,
                existing=None,
                admission=admission,
                sandbox_uid=uid,
            )
            self.assertTrue(entered.wait(timeout=2))
            second = provisioners[1].ensure_job(
                experiment_id="",
                project_id=self.project_id,
                req=req,
                existing=None,
                admission=admission,
                sandbox_uid=uid,
            )
            self.assertIsNone(second)
            release.set()
            first_job = first.result(timeout=2)
        self.assertIsNotNone(first_job)
        first_job.done.wait(timeout=2)
        self.assertEqual(len(self.backend.acquired), 1)

    def test_cleanup_owner_blocks_second_settler_and_replacement(self) -> None:
        exp_id = self._experiment()
        uid = "uid_cleanup_owner"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=uid,
            project_id=self.project_id,
            status="provisioning",
            provision_claim="claim-old",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        self.backend.alive["sb-old"] = True
        self.backend.by_experiment[uid] = "sb-old"
        entered = threading.Event()
        release = threading.Event()
        original_terminate = self.backend.terminate

        def terminate(*, sandbox_id: str) -> bool:
            if sandbox_id == "sb-old":
                entered.set()
                release.wait(timeout=2)
            return original_terminate(sandbox_id=sandbox_id)

        self.backend.terminate = terminate  # type: ignore[method-assign]
        stale = self.app.sandboxes.registry.get_by_uid(sandbox_uid=uid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(
                self.app.sandboxes.lifecycle.settle_provisioning,
                row=stale,
                status="failed",
                error="old failed",
            )
            self.assertTrue(entered.wait(timeout=2))
            self.assertFalse(
                self.app.sandboxes.lifecycle.settle_provisioning(
                    row=stale, status="failed", error="stale failed"
                )
            )
            with self.app.store.transaction() as conn:
                self.assertFalse(
                    self.app.sandboxes.registry.claim_provisioning(
                        conn=conn,
                        experiment_id=exp_id,
                        sandbox_uid=uid,
                        claim_token="claim-new",
                        project_id=self.project_id,
                        status="provisioning",
                    )
                )
            release.set()
            self.assertTrue(winner.result(timeout=2))

        with self.app.store.transaction() as conn:
            self.assertTrue(
                self.app.sandboxes.registry.claim_provisioning(
                    conn=conn,
                    experiment_id=exp_id,
                    sandbox_uid=uid,
                    claim_token="claim-new",
                    project_id=self.project_id,
                    status="provisioning",
                )
            )
        self.backend.alive["sb-new"] = True
        self.backend.by_experiment[uid] = "sb-new"
        self.app.sandboxes.registry.update_claimed(
            experiment_id=exp_id,
            sandbox_uid=uid,
            claim_token="claim-new",
            sandbox_id="sb-new",
        )
        self.assertFalse(
            self.app.sandboxes.lifecycle.settle_provisioning(
                row=stale, status="failed", error="very stale failed"
            )
        )
        fresh = self.app.sandboxes.registry.get_by_uid(sandbox_uid=uid)
        self.assertEqual(fresh["provision_claim"], "claim-new")
        self.assertTrue(self.backend.alive["sb-new"])

    def test_running_cleanup_owner_blocks_stale_releaser(self) -> None:
        exp_id = self._experiment()
        uid = "uid_running_cleanup"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=uid,
            project_id=self.project_id,
            status="running",
            sandbox_id="sb-old",
        )
        self.backend.alive["sb-old"] = True
        entered = threading.Event()
        release = threading.Event()
        original_terminate = self.backend.terminate

        def terminate(*, sandbox_id: str) -> bool:
            if sandbox_id == "sb-old":
                entered.set()
                release.wait(timeout=2)
            return original_terminate(sandbox_id=sandbox_id)

        self.backend.terminate = terminate  # type: ignore[method-assign]
        stale = self.app.sandboxes.registry.get_by_uid(sandbox_uid=uid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            winner = pool.submit(
                self.app.sandboxes.lifecycle.settle_running, row=stale
            )
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(
                self.app.sandboxes.lifecycle.settle_running(row=stale), "lost"
            )
            release.set()
            self.assertEqual(winner.result(timeout=2), "stopped")

        with self.app.store.transaction() as conn:
            self.assertTrue(
                self.app.sandboxes.registry.claim_provisioning(
                    conn=conn,
                    experiment_id=exp_id,
                    sandbox_uid=uid,
                    claim_token="claim-new",
                    project_id=self.project_id,
                    status="provisioning",
                )
            )
        self.backend.alive["sb-new"] = True
        self.app.sandboxes.registry.update_claimed(
            experiment_id=exp_id,
            sandbox_uid=uid,
            claim_token="claim-new",
            sandbox_id="sb-new",
        )

        self.assertEqual(
            self.app.sandboxes.lifecycle.settle_running(row=stale), "lost"
        )
        fresh = self.app.sandboxes.registry.get_by_uid(sandbox_uid=uid)
        self.assertEqual(fresh["provision_claim"], "claim-new")
        self.assertTrue(self.backend.alive["sb-new"])

    def test_stale_release_does_not_cancel_replacement_job(self) -> None:
        self.app.sandboxes.request_wait_seconds = 0.05
        self.backend.gate = threading.Event()
        uid = "uid_replacement_job"
        replacement = self.call(
            "sandbox.request", project_id=self.project_id, sandbox_uid=uid
        )
        job = self.app.sandboxes.provisioner._jobs[uid]
        stale = {
            **self.app.sandboxes.registry.get_by_uid(sandbox_uid=uid),
            "status": "running",
            "sandbox_id": "sb-old",
            "provision_claim": "",
        }

        result = self.app.sandboxes._release_row(row=stale)

        self.assertEqual(result["sandbox_uid"], replacement["sandbox_uid"])
        self.assertFalse(job.cancel.is_set())
        self.backend.gate.set()
        self.assertTrue(job.done.wait(timeout=2))
        fresh = self.app.sandboxes.registry.get_by_uid(sandbox_uid=uid)
        self.assertEqual(fresh["status"], "running")

    def test_cross_project_uid_race_cannot_return_foreign_sandbox(self) -> None:
        other = self.call("project", action="create", name="Other Project")["id"]
        uid = "uid_cross_project"

        def race(*, sandbox_uid: str):
            self.app.sandboxes.registry.upsert(
                experiment_id="",
                sandbox_uid=sandbox_uid,
                project_id=self.project_id,
                status="provisioning",
                provision_claim="foreign-claim",
            )
            raise NotFoundError(f"sandbox not found: {sandbox_uid}")

        with patch.object(self.app.sandboxes.registry, "get_by_uid", side_effect=race):
            with self.assertRaises(NotFoundError):
                self.app.sandboxes.request(
                    project_id=other,
                    sandbox_uid=uid,
                    public_key="ssh-ed25519 AAAA race@test",
                    include_data_plane_enrichment=False,
                )
        self.assertEqual(self.backend.acquired, [])

    def test_provisioning_failure_marks_failed_and_cleans_up(self) -> None:
        self.app.sandboxes.request_wait_seconds = 2.0
        self.backend.fail_after_create = True
        exp_id = self._experiment()
        result = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["error"])
        # The sandbox that was created before the tunnel failure got terminated.
        self.assertTrue(self.backend.terminated)

    def test_provisioning_failure_stays_retryable_until_vm_is_confirmed_gone(self) -> None:
        self.backend.fail_after_create = True
        self.backend.terminate = lambda *, sandbox_id: False  # type: ignore[method-assign]
        exp_id = self._experiment()

        result = self.call(
            "sandbox.request", project_id=self.project_id, experiment_id=exp_id
        )

        self.assertEqual(result["status"], "provisioning")
        self.assertEqual(result["phase"], "cleanup")
        self.assertTrue(self.backend.alive[str(result["sandbox_id"])])

    def test_release_cancels_provisioning(self) -> None:
        self.app.sandboxes.request_wait_seconds = 0.05
        self.backend.gate = threading.Event()
        exp_id = self._experiment()
        started = self.call("sandbox.request", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(started["status"], "provisioning")
        self.call(
            "sandbox.release",
            project_id=self.project_id,
            experiment_id=exp_id,
            confirm_retained=True,
        )
        # Let the gated job unwind; it must honor the cancel, not go running.
        self.backend.gate.set()
        final = self._await_sandbox_status(started["sandbox_uid"], "terminated")
        self.assertEqual(final["status"], "terminated")

    def test_get_reconciles_orphaned_provisioning(self) -> None:
        # A provisioning row with no in-flight job (daemon restart mid-provision)
        # must reconcile to failed so a polling agent doesn't wait forever.
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid="uid_unclaimed_orphan",
            project_id=self.project_id,
            status="provisioning",
            provision_claim="",
        )
        result = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "failed")

    def test_request_does_not_replace_sandbox_with_uncertain_cleanup(self) -> None:
        exp_id = self._experiment()
        sandbox_uid = "uid_cleanup_retry"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            project_id=self.project_id,
            status="provisioning",
            phase="cleanup",
        )
        self.backend.find_sandbox_id = (  # type: ignore[method-assign]
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down"))
        )

        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            provision_claim="remote-claim",
        )
        result = self.call(
            "sandbox.request",
            project_id=self.project_id,
            experiment_id=exp_id,
        )

        self.assertEqual(self.backend.acquired, [])
        self.assertEqual(result["status"], "provisioning")
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=sandbox_uid)
        self.assertEqual(row["status"], "provisioning")
        self.assertEqual(row["phase"], "cleanup")

    def test_cross_process_poll_preserves_claimed_provision(self) -> None:
        exp_id = self._experiment()
        self.app.sandboxes.provisioner.begin_provisioning_row(
            experiment_id=exp_id,
            project_id=self.project_id,
            req=SandboxRequest(
                experiment_id=exp_id, project_id=self.project_id, public_key="k"
            ),
            claim_token="remote-claim",
        )
        result = self.call(
            "sandbox.get", project_id=self.project_id, experiment_id=exp_id
        )
        self.assertEqual(result["status"], "provisioning")
        self.assertEqual(self.backend.terminated, [])

    def test_get_returns_none_when_never_requested(self) -> None:
        exp_id = self._experiment()
        result = self.call("sandbox.get", project_id=self.project_id, experiment_id=exp_id)
        self.assertEqual(result["status"], "none")


if __name__ == "__main__":
    unittest.main()
