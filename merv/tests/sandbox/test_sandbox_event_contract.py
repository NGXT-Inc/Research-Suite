from __future__ import annotations

import inspect
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.support.brain import DEFAULT_PUBLIC_KEY, TestBrain
from merv.brain.kernel.state.store import StateStore
from merv.brain.kernel.utils import format_iso, now_iso
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.sandbox.repository import SandboxRepository
from merv.brain.sandbox.sandbox_runs import SandboxRunLedger
from merv.brain.sandbox.facade import SandboxFacade


PUBLIC_SIGNATURES = {
    "request": "(self, *, experiment_id: 'str | None' = None, project_id: 'str | None' = None, gpu: 'str | None' = None, cpu: 'float | None' = None, memory: 'int | None' = None, time_limit: 'int | None' = None, instance_type: 'str | None' = None, region: 'str | None' = None, provider: 'str | None' = None, public_key: 'str | None' = None, public_key_override: 'str | None' = None, include_data_plane_enrichment: 'bool' = True, additional: 'bool' = False, sandbox_uid: 'str | None' = None, provisioning_user_id: 'str' = '', provisioning_key_id: 'str' = '') -> 'dict[str, Any]'",
    "request_from_data_plane": "(self, *, experiment_id: 'str | None' = None, public_key: 'str', project_id: 'str | None' = None, gpu: 'str | None' = None, cpu: 'float | None' = None, memory: 'int | None' = None, time_limit: 'int | None' = None, instance_type: 'str | None' = None, region: 'str | None' = None, provider: 'str | None' = None, additional: 'bool' = False, sandbox_uid: 'str | None' = None, provisioning_user_id: 'str' = '') -> 'dict[str, Any]'",
    "pull_outputs_command": "(self, *, experiment_id: 'str | None' = None, project_id: 'str | None' = None, sandbox_uid: 'str | None' = None, paths: 'list[str] | None' = None) -> 'dict[str, Any]'",
    "get": "(self, *, experiment_id: 'str | None' = None, project_id: 'str | None' = None, tenant_id: 'str | None' = None, sandbox_uid: 'str | None' = None, include_data_plane_enrichment: 'bool' = True) -> 'dict[str, Any]'",
    "attach": "(self, *, experiment_id: 'str', project_id: 'str | None' = None, sandbox_uid: 'str', include_data_plane_enrichment: 'bool' = True, public_key_override: 'str | None' = None) -> 'dict[str, Any]'",
    "attach_from_data_plane": "(self, *, experiment_id: 'str', sandbox_uid: 'str', public_key: 'str', project_id: 'str | None' = None) -> 'dict[str, Any]'",
    "extend": "(self, *, experiment_id: 'str | None' = None, project_id: 'str | None' = None, tenant_id: 'str | None' = None, sandbox_uid: 'str | None' = None, seconds: 'int' = 1800) -> 'dict[str, Any]'",
    "options": "(self, *, project_id: 'str | None' = None, gpu: 'str | None' = None, region: 'str | None' = None) -> 'dict[str, Any]'",
    "list_sandboxes": "(self, *, project_id: 'str | None' = None) -> 'dict[str, Any]'",
    "release": "(self, *, experiment_id: 'str | None' = None, project_id: 'str | None' = None, sandbox_uid: 'str | None' = None, confirm_retained: 'bool' = False) -> 'dict[str, Any]'",
    "terminal": "(self, *, experiment_id: 'str | None' = None, project_id: 'str | None' = None, sandbox_uid: 'str | None' = None, tail: 'int | None' = None, since: 'int | None' = None) -> 'dict[str, Any]'",
    "runs": "(self, *, experiment_id: 'str | None' = None, project_id: 'str | None' = None, tenant_id: 'str | None' = None, sandbox_uid: 'str | None' = None, wait_seconds: 'int' = 0) -> 'dict[str, Any]'",
}


def _quiet_sample() -> dict:
    return {
        "cpu": {"used_cores": 0.0, "limit_cores": 2.0},
        "memory": {"used_bytes": 1_000_000, "limit_bytes": None},
        "network": {"bytes_total": 1_000, "ssh_established": 0},
        "gpus": [{"index": 0, "util_pct": 0}],
    }


class SandboxFacadeSignatureContractTest(unittest.TestCase):
    def test_public_signatures_and_defaults_are_exact(self) -> None:
        actual = {
            name: str(inspect.signature(getattr(SandboxFacade, name)))
            for name in PUBLIC_SIGNATURES
        }
        self.assertEqual(actual, PUBLIC_SIGNATURES)


class SandboxEventContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        repo = Path(self.tmp.name)
        self.backend = FakeSandboxBackend()
        self.app = TestBrain(
            repo_root=repo,
            db_path=repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.project_id = self.app.call_tool(
            "project", {"action": "create", "name": "Event Contract"}
        )["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _experiment(self, name: str) -> str:
        experiment_id = self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": name, "intent": "contract"},
        )["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?",
                (experiment_id,),
            )
        return str(experiment_id)

    def _request(self, experiment_id: str) -> dict:
        return self.app.sandboxes.request(
            project_id=self.project_id,
            experiment_id=experiment_id,
            public_key=DEFAULT_PUBLIC_KEY,
            gpu="A100",
            time_limit=1200,
        )

    def _events(self) -> list[dict]:
        events = self.app.store.recent_events(project_id=self.project_id)["events"]
        return list(reversed([event for event in events if event["type"].startswith("sandbox.")]))

    def _assert_event(
        self,
        event: dict,
        event_type: str,
        payload: dict,
        *,
        volatile: set[str] = frozenset(),
    ) -> None:
        self.assertEqual(event["type"], event_type)
        self.assertEqual(set(event["payload"]), set(payload))
        self.assertEqual(
            {key: value for key, value in event["payload"].items() if key not in volatile},
            {key: value for key, value in payload.items() if key not in volatile},
        )

    def test_create_persists_row_then_generation_then_event(self) -> None:
        experiment_id = self._experiment("create-order")
        repository = self.app.sandboxes.repository
        trace: list[str] = []
        original_upsert = repository.upsert
        original_generation = repository.record_generation
        original_emit = repository.emit_event

        def upsert(**kwargs):
            if kwargs.get("status") == "running":
                trace.append("running-row")
            return original_upsert(**kwargs)

        def record_generation(**kwargs):
            trace.append("generation")
            return original_generation(**kwargs)

        def emit_event(**kwargs):
            if kwargs.get("event_type") == "sandbox.created":
                trace.append("event")
            return original_emit(**kwargs)

        with (
            patch.object(repository, "upsert", side_effect=upsert),
            patch.object(repository, "record_generation", side_effect=record_generation),
            patch.object(repository, "emit_event", side_effect=emit_event),
        ):
            created = self._request(experiment_id)

        self.assertEqual(trace, ["running-row", "generation", "event"])
        self._assert_event(
            self._events()[-1],
            "sandbox.created",
            {
                "sandbox_id": created["sandbox_id"],
                "gpu": "A100",
                "instance_type": "",
                "region": "",
                "time_limit": 1200,
            },
        )

    def test_reuse_attach_and_extend_events(self) -> None:
        source = self._experiment("source")
        target = self._experiment("target")
        created = self._request(source)
        reused = self._request(source)
        attached = self.app.sandboxes.attach(
            project_id=self.project_id,
            experiment_id=target,
            sandbox_uid=created["sandbox_uid"],
        )
        self.app.sandboxes.repository.record_command_snapshot(
            sandbox_uid=created["sandbox_uid"],
            snapshot={"command_id": "cmd", "status": "running"},
        )
        extended = self.app.sandboxes.extend(
            project_id=self.project_id, sandbox_uid=created["sandbox_uid"], seconds=600
        )
        events = self._events()

        self.assertEqual([event["type"] for event in events], [
            "sandbox.created",
            "sandbox.reused",
            "sandbox.attached",
            "sandbox.lifetime_extended",
        ])
        self._assert_event(
            events[1],
            "sandbox.reused",
            {
                "sandbox_id": reused["sandbox_id"],
                "sandbox_uid": reused["sandbox_uid"],
                "active_experiment_ids": [source],
            },
        )
        self._assert_event(
            events[2],
            "sandbox.attached",
            {
                "sandbox_id": attached["sandbox_id"],
                "sandbox_uid": attached["sandbox_uid"],
                "active_experiment_ids": attached["active_experiment_ids"],
            },
        )
        self._assert_event(
            events[3],
            "sandbox.lifetime_extended",
            {
                "sandbox_id": extended["sandbox_id"],
                "sandbox_uid": extended["sandbox_uid"],
                "old_expires_at": "",
                "expires_at": "",
                "seconds": 600,
                "time_limit": 1800,
            },
            volatile={"old_expires_at", "expires_at"},
        )

    def test_release_success_and_failure_order_events_and_row_state(self) -> None:
        for outcome in ("stopped", "maybe_alive"):
            with self.subTest(outcome=outcome):
                experiment_id = self._experiment(f"release-{outcome}")
                created = self._request(experiment_id)
                trace: list[str] = []
                provisioner = self.app.sandboxes.provisioner
                lifecycle = self.app.sandboxes.lifecycle
                repository = self.app.sandboxes.repository
                original_cancel = provisioner.cancel
                original_terminate = lifecycle.terminate_vm
                original_apply = lifecycle.apply
                original_emit = repository.emit_event

                def cancel(**kwargs):
                    trace.append("cancel")
                    return original_cancel(**kwargs)

                def terminate(**kwargs):
                    trace.append("terminate")
                    return original_terminate(**kwargs)

                def apply(**kwargs):
                    trace.append("apply")
                    return original_apply(**kwargs)

                def emit(**kwargs):
                    trace.append("event")
                    current = repository.get_by_uid(sandbox_uid=created["sandbox_uid"])
                    expected = "running" if outcome == "maybe_alive" else "terminated"
                    self.assertEqual(current["status"], expected)
                    return original_emit(**kwargs)

                terminate_patch = (
                    patch.object(self.backend, "terminate", return_value=False)
                    if outcome == "maybe_alive"
                    else patch.object(self.backend, "terminate", wraps=self.backend.terminate)
                )
                with (
                    terminate_patch,
                    patch.object(provisioner, "cancel", side_effect=cancel),
                    patch.object(lifecycle, "terminate_vm", side_effect=terminate),
                    patch.object(lifecycle, "apply", side_effect=apply),
                    patch.object(repository, "emit_event", side_effect=emit),
                ):
                    self.app.sandboxes.release(
                        project_id=self.project_id,
                        experiment_id=experiment_id,
                        confirm_retained=True,
                    )

                self.assertEqual(trace, ["cancel", "terminate", "apply", "event"])
                expected_type = (
                    "sandbox.release_failed" if outcome == "maybe_alive" else "sandbox.released"
                )
                expected_payload = (
                    {
                        "sandbox_id": created["sandbox_id"],
                        "sandbox_uid": created["sandbox_uid"],
                        "reason": "terminate failed; instance may still be alive",
                    }
                    if outcome == "maybe_alive"
                    else {
                        "sandbox_id": created["sandbox_id"],
                        "sandbox_uid": created["sandbox_uid"],
                        "active_experiment_ids": [experiment_id],
                        "stopped": True,
                    }
                )
                self._assert_event(self._events()[-1], expected_type, expected_payload)

    def test_reconcile_death_applies_terminal_intent_before_event(self) -> None:
        experiment_id = self._experiment("reconcile")
        created = self._request(experiment_id)
        self.backend.alive[created["sandbox_id"]] = False
        lifecycle = self.app.sandboxes.lifecycle
        repository = self.app.sandboxes.repository
        trace: list[str] = []
        original_mark = lifecycle.mark_terminated
        original_emit = repository.emit_event

        def mark(**kwargs):
            trace.append("intent")
            return original_mark(**kwargs)

        def emit(**kwargs):
            trace.append("event")
            self.assertEqual(
                repository.get_by_uid(sandbox_uid=created["sandbox_uid"])["status"],
                "terminated",
            )
            return original_emit(**kwargs)

        with (
            patch.object(lifecycle, "mark_terminated", side_effect=mark),
            patch.object(repository, "emit_event", side_effect=emit),
        ):
            self.app.sandboxes.get(
                project_id=self.project_id, experiment_id=experiment_id
            )

        self.assertEqual(trace, ["intent", "event"])
        self._assert_event(
            self._events()[-1],
            "sandbox.expired",
            {"sandbox_id": created["sandbox_id"], "sandbox_uid": created["sandbox_uid"]},
        )

    def test_expiry_and_idle_reap_event_payloads(self) -> None:
        expiry_exp = self._experiment("expiry")
        expired = self._request(expiry_exp)
        self.assertEqual(
            self.app.sandboxes.reap_expired(now=datetime(2999, 1, 1, tzinfo=UTC)), 1
        )
        self._assert_event(
            self._events()[-1],
            "sandbox.expired",
            {
                "sandbox_id": expired["sandbox_id"],
                "sandbox_uid": expired["sandbox_uid"],
                "reaped": True,
                "expires_at": "",
                "stopped": True,
            },
            volatile={"expires_at"},
        )

        idle_exp = self._experiment("idle")
        idle = self._request(idle_exp)
        now = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        idle_since = now - timedelta(hours=1)
        self.app.sandboxes.repository.record_heartbeat(
            experiment_id=idle_exp,
            sandbox_uid=idle["sandbox_uid"],
            idle_since=format_iso(idle_since),
            snapshot={
                "sampled_at": format_iso(now - timedelta(seconds=30)),
                "metrics": _quiet_sample(),
            },
        )
        self.backend.metrics[idle["sandbox_id"]] = _quiet_sample()
        self.assertEqual(
            self.app.sandboxes.reap_idle(now=now, threshold_seconds=3600), 1
        )
        self._assert_event(
            self._events()[-1],
            "sandbox.idle_reaped",
            {
                "sandbox_id": idle["sandbox_id"],
                "sandbox_uid": idle["sandbox_uid"],
                "reaped": True,
                "expires_at": "",
                "stopped": True,
                "idle_since": "",
                "idle_seconds": 3600,
                "threshold_seconds": 3600,
            },
            volatile={"expires_at", "idle_since"},
        )

    def test_stale_and_failed_provision_events_follow_terminal_write(self) -> None:
        stale_exp = self._experiment("stale")
        uid = "sbx_stale_contract"
        self.app.sandboxes.repository.upsert(
            experiment_id=stale_exp,
            sandbox_uid=uid,
            project_id=self.project_id,
            sandbox_id="sb-stale-contract",
            status="provisioning",
            phase="connecting",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(
            self.app.sandboxes.reap_stale_provisions(
                now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC), deadline_seconds=60
            ),
            1,
        )
        self.assertEqual(
            self.app.sandboxes.repository.get_by_uid(sandbox_uid=uid)["status"],
            "failed",
        )
        self._assert_event(
            self._events()[-1],
            "sandbox.failed",
            {
                "error": "stale provision reaped",
                "phase": "connecting",
                "sandbox_id": "sb-stale-contract",
            },
        )

        failed_exp = self._experiment("failed")
        self.backend.fail_after_create = True
        failed = self._request(failed_exp)
        self.assertEqual(failed["status"], "failed")
        self._assert_event(
            self._events()[-1],
            "sandbox.failed",
            {"error": "fake tunnel failure"},
        )


class SandboxRepositoryEventContractScenarios:
    """The same persistence/event transaction scenario for both SQL dialects."""

    store: StateStore

    def _seed_project(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, summary, created_at) VALUES (?, ?, ?, ?)",
                ("proj_event_contract", "Events", "", now_iso()),
            )

    def test_repository_event_uses_its_own_transaction(self) -> None:
        self._seed_project()
        repository = SandboxRepository(store=self.store)
        transactions: list[str] = []
        original_transaction = self.store.transaction

        @contextmanager
        def observed_transaction():
            transactions.append("enter")
            with original_transaction() as conn:
                yield conn
            transactions.append("commit")

        with patch.object(self.store, "transaction", side_effect=observed_transaction):
            repository.emit_event(
                project_id="proj_event_contract",
                event_type="sandbox.contract",
                experiment_id="exp_contract",
                payload={"sandbox_uid": "sbx_contract", "nested": {"ok": True}},
            )

        self.assertEqual(transactions, ["enter", "commit"])
        event = self.store.recent_events(project_id="proj_event_contract")["events"][0]
        self.assertEqual(event["type"], "sandbox.contract")
        self.assertEqual(
            event["payload"], {"sandbox_uid": "sbx_contract", "nested": {"ok": True}}
        )

    def test_run_finished_flag_and_event_share_one_transaction(self) -> None:
        self._seed_project()
        repository = SandboxRepository(store=self.store)
        repository.upsert(
            experiment_id="",
            sandbox_uid="sbx_run_contract",
            project_id="proj_event_contract",
            sandbox_id="sb-run-contract",
            status="running",
        )
        ledger = SandboxRunLedger(
            store=self.store,
            repository=repository,
            backend=object(),  # type: ignore[arg-type]
            mgmt_keys=object(),  # type: ignore[arg-type]
        )
        original_record_event = self.store.record_event
        observations: list[int] = []

        def observed_record_event(*, conn, **kwargs):
            row = conn.execute(
                "SELECT finished_event_emitted FROM sandbox_runs "
                "WHERE sandbox_uid = ? AND label = ?",
                ("sbx_run_contract", "seed0"),
            ).fetchone()
            observations.append(int(row["finished_event_emitted"]))
            return original_record_event(conn=conn, **kwargs)

        with patch.object(self.store, "record_event", side_effect=observed_record_event):
            ledger._record(
                row={
                    "sandbox_uid": "sbx_run_contract",
                    "sandbox_id": "sb-run-contract",
                    "project_id": "proj_event_contract",
                    "experiment_id": "",
                },
                listing=[
                    {
                        "label": "seed0",
                        "command": "python train.py",
                        "pid": 42,
                        "exit_code": 0,
                        "started_at": "2026-01-01T00:00:00Z",
                        "finished_at": "2026-01-01T00:01:00Z",
                    }
                ],
            )

        self.assertEqual(observations, [1])
        event = self.store.recent_events(project_id="proj_event_contract")["events"][0]
        self.assertEqual(event["type"], "run.finished")
        self.assertEqual(
            event["payload"],
            {
                "sandbox_uid": "sbx_run_contract",
                "label": "seed0",
                "exit_code": 0,
                "finished_at": "2026-01-01T00:01:00Z",
            },
        )


class SqliteSandboxRepositoryEventContractTest(
    SandboxRepositoryEventContractScenarios, unittest.TestCase
):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(db_path=Path(self.tmp.name) / "state.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
