"""Sync leases: the exclusive per-experiment byte-movement authority.

Phase 4 of docs/CLOUD_BACKEND_MIGRATION_PLAN.md (fixed decision 8): exclusive
acquire, same-holder renewal, TTL + takeover, lease-checked completion
reports, lease visibility in the agent views, and the local-mode guarantee
that a single implicit holder never changes the agent surface.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app import ResearchPluginApp
from backend.dataplane.state import CLIENT_ID_ENV, SandboxLocalState
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.sync_sessions import (
    InProcessControlPlaneView,
    LeaseService,
    SyncSessionService,
    build_sync_session,
)
from backend.state.store import StateStore
from backend.utils import PermissionDeniedError
from tests.fakes import FakeRsyncSyncer


class LeaseServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(db_path=Path(self.tmp.name) / "state.sqlite")
        self.leases = LeaseService(store=self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_acquire_is_exclusive_while_live(self) -> None:
        lease = self.leases.acquire(experiment_id="exp_1", holder_client_id="client_a")
        self.assertTrue(lease["id"])
        self.assertEqual(lease["holder_client_id"], "client_a")
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.leases.acquire(experiment_id="exp_1", holder_client_id="client_b")
        # The refusal is actionable: it names the holder and the expiry.
        self.assertIn("client_a", str(ctx.exception))
        self.assertIn(lease["expires_at"], str(ctx.exception))

    def test_same_holder_reacquire_renews_in_place(self) -> None:
        first = self.leases.acquire(
            experiment_id="exp_1", holder_client_id="client_a", ttl_seconds=60
        )
        second = self.leases.acquire(
            experiment_id="exp_1", holder_client_id="client_a", ttl_seconds=600
        )
        # A renewal, not a new grant: the lease id is stable, the expiry moves.
        self.assertEqual(second["id"], first["id"])
        self.assertGreater(second["expires_at"], first["expires_at"])

    def test_expired_lease_takeover_invalidates_the_old_holder(self) -> None:
        old = self.leases.acquire(
            experiment_id="exp_1", holder_client_id="client_a", ttl_seconds=0
        )
        taken = self.leases.acquire(
            experiment_id="exp_1", holder_client_id="client_b"
        )
        self.assertNotEqual(taken["id"], old["id"])
        self.assertEqual(taken["holder_client_id"], "client_b")
        # The superseded holder can neither renew nor report completion.
        with self.assertRaises(PermissionDeniedError):
            self.leases.renew(experiment_id="exp_1", lease_id=old["id"])
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.leases.validate_completion(experiment_id="exp_1", lease_id=old["id"])
        self.assertIn("stale", str(ctx.exception))
        self.assertIn("client_b", str(ctx.exception))

    def test_completion_report_with_stale_lease_id_rejected(self) -> None:
        live = self.leases.acquire(experiment_id="exp_1", holder_client_id="client_a")
        # The live lease validates; a foreign/stale id does not.
        self.leases.validate_completion(experiment_id="exp_1", lease_id=live["id"])
        with self.assertRaises(PermissionDeniedError):
            self.leases.validate_completion(
                experiment_id="exp_1", lease_id="lease_forged"
            )
        # An experiment that never had a lease rejects any report.
        with self.assertRaises(PermissionDeniedError):
            self.leases.validate_completion(
                experiment_id="exp_never", lease_id=live["id"]
            )

    def test_release_drops_the_lease_and_refuses_foreign_ids(self) -> None:
        lease = self.leases.acquire(experiment_id="exp_1", holder_client_id="client_a")
        with self.assertRaises(PermissionDeniedError):
            self.leases.release(experiment_id="exp_1", lease_id="lease_other")
        self.leases.release(experiment_id="exp_1", lease_id=lease["id"])
        self.assertIsNone(self.leases.holder(experiment_id="exp_1"))
        # Idempotent once gone.
        self.leases.release(experiment_id="exp_1", lease_id=lease["id"])
        # Freed: another client acquires immediately.
        taken = self.leases.acquire(experiment_id="exp_1", holder_client_id="client_b")
        self.assertEqual(taken["holder_client_id"], "client_b")

    def test_session_payload_shape(self) -> None:
        lease = self.leases.acquire(experiment_id="exp_1", holder_client_id="client_a")
        session = build_sync_session(
            experiment_id="exp_1",
            sandbox_id="sb-1",
            ssh_host="host.test",
            ssh_port=2222,
            ssh_user="root",
            experiment_dir="/workspace/exp-one",
            lease=lease,
        )
        self.assertEqual(session["schema_version"], 1)
        self.assertEqual(session["transfer_contract_version"], 1)
        self.assertEqual(
            session["ssh"], {"host": "host.test", "port": 2222, "user": "root"}
        )
        self.assertEqual(
            session["remote"],
            {
                "experiment_dir": "/workspace/exp-one",
                "data_dir": "/workspace/data",
                "sessions_dir": "/workspace/.research_plugin_sessions/exp_1",
                "artifacts_to_keep": "/workspace/exp-one/artifacts_to_keep",
            },
        )
        self.assertEqual(
            session["direction_policy"],
            {
                "experiment_dir": "remote_authoritative_for_results",
                "artifacts_to_keep": "remote_append_only",
            },
        )
        self.assertEqual(session["lease"]["id"], lease["id"])
        self.assertEqual(session["lease"]["holder_client_id"], "client_a")


class ClientIdentityTest(unittest.TestCase):
    def test_client_id_is_generated_once_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "dataplane_state.sqlite"
            first = SandboxLocalState(db_path=db_path).client_id()
            self.assertTrue(first.startswith("client_"))
            # A fresh handle on the same machine state sees the same identity.
            again = SandboxLocalState(db_path=db_path).client_id()
            self.assertEqual(again, first)

    def test_env_override_wins_without_touching_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "dataplane_state.sqlite"
            state = SandboxLocalState(db_path=db_path)
            persisted = state.client_id()
            with mock.patch.dict("os.environ", {CLIENT_ID_ENV: "client_override"}):
                self.assertEqual(state.client_id(), "client_override")
            self.assertEqual(state.client_id(), persisted)


class LeaseAgentSurfaceTest(unittest.TestCase):
    """Leases through the tool surface: visible in views, silent in local mode."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.project_id = self.call("project.create", name="Lease Project")["id"]
        exp = self.call(
            "experiment.create", name="exp-1", project_id=self.project_id, intent="x"
        )
        self.exp_id = exp["id"]
        with self.app.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?",
                (self.exp_id,),
            )

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def test_lease_visible_in_sandbox_get_and_list(self) -> None:
        self.call("sandbox.request", project_id=self.project_id, experiment_id=self.exp_id)
        client_id = self.app.worker.client_id()
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=self.exp_id)
        self.assertEqual(got["sync_lease"]["holder_client_id"], client_id)
        self.assertTrue(got["sync_lease"]["expires_at"])
        listed = self.call("sandbox.list", project_id=self.project_id)["sandboxes"]
        self.assertEqual(listed[0]["sync_lease"]["holder_client_id"], client_id)

    def test_local_mode_single_implicit_holder_is_a_noop_for_the_agent(self) -> None:
        # One client re-syncing its own experiment renews its own lease —
        # no error, no extra ceremony, same response shape as ever.
        self.call("sandbox.request", project_id=self.project_id, experiment_id=self.exp_id)
        first = self.call("sandbox.sync", project_id=self.project_id, experiment_id=self.exp_id)
        second = self.call("sandbox.sync", project_id=self.project_id, experiment_id=self.exp_id)
        self.assertEqual(first["sync"]["provider"], "ssh_rsync")
        self.assertEqual(second["sync"]["provider"], "ssh_rsync")
        released = self.call(
            "sandbox.release", project_id=self.project_id, experiment_id=self.exp_id
        )
        self.assertEqual(released["status"], "terminated")

    def test_second_client_cannot_double_sync_a_leased_experiment(self) -> None:
        self.call("sandbox.request", project_id=self.project_id, experiment_id=self.exp_id)
        # Hand the lease to a simulated second client (long TTL), as if
        # another machine's daemon were syncing this experiment.
        own = self.app.sandboxes.leases.holder(experiment_id=self.exp_id)
        self.app.sandboxes.leases.release(
            experiment_id=self.exp_id, lease_id=str(own["lease_id"])
        )
        foreign = self.app.sandboxes.leases.acquire(
            experiment_id=self.exp_id,
            holder_client_id="client_elsewhere",
            ttl_seconds=3600,
        )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.call(
                "sandbox.sync", project_id=self.project_id, experiment_id=self.exp_id
            )
        self.assertIn("client_elsewhere", str(ctx.exception))
        # And sandbox.get tells this client why it isn't syncing.
        got = self.call("sandbox.get", project_id=self.project_id, experiment_id=self.exp_id)
        self.assertEqual(got["sync_lease"]["holder_client_id"], "client_elsewhere")
        self.assertEqual(got["sync_lease"]["expires_at"], foreign["expires_at"])
        # The poller's view skips the row too — no targets to double-sync.
        self.assertEqual(self.app.sandboxes.control_view.sync_targets(), [])


class InProcessControlPlaneViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(db_path=Path(self.tmp.name) / "state.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sync_targets_only_requires_running_row_source(self) -> None:
        class _Rows:
            def list_running_sync_rows(self) -> list[dict[str, object]]:
                return [
                    {
                        "experiment_id": "exp_1",
                        "sandbox_id": "sb-1",
                        "ssh_host": "host.test",
                        "ssh_port": 2222,
                        "ssh_user": "root",
                        "sync_dir": "/workspace/exp-one",
                        "tenant_id": "tenant_a",
                    }
                ]

        leases = LeaseService(store=self.store)
        sessions = SyncSessionService(leases=leases, client_id="client_a")
        view = InProcessControlPlaneView(registry=_Rows(), sessions=sessions)

        targets = view.sync_targets(tenant_id="tenant_a")

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["row"]["sandbox_id"], "sb-1")
        self.assertEqual(targets[0]["session"]["experiment_id"], "exp_1")
        self.assertEqual(
            targets[0]["session"]["lease"]["holder_client_id"], "client_a"
        )
        self.assertEqual(view.sync_targets(tenant_id="tenant_b"), [])


if __name__ == "__main__":
    unittest.main()
