"""Cloud cleanup sweeps (cloud plan Phase 9), driven by injected clocks.

The idempotent sweeps grouped behind CleanupService.run_all — orphan-VM,
blob TTL GC, storage TTL GC, and stale-provision reap — each take a
``now`` so the test owns the clock. The service is mode-blind (the in-process
app exercises the exact code the control plane schedules), so these run without
docker or a real control plane.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.support.brain import TestBrain
from backend.execution.backends.fake import FakeSandboxBackend
from backend.sandbox.sandbox_backend import BackendCapabilities
from backend.services.cleanup import CleanupService


class CleanupSweepTest(unittest.TestCase):
    # Park the background reaper so the test, not a timer, drives every sweep.
    _ENV = {
        "RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL": "3600",
        "RESEARCH_PLUGIN_SANDBOX_REAPER": "0",
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in self._ENV}
        os.environ.update(self._ENV)
        self.backend = FakeSandboxBackend()
        # enforce_expiry off keeps the reaper inert; the sweeps drive themselves.
        self.backend.capabilities = BackendCapabilities(name="fake")
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.store = self.app.store
        self.cleanup = CleanupService(
            sandboxes=self.app.sandboxes, blobs=self.app.blobs
        )
        self.project_id = self.app.call_tool("project", {"action": "create", "name": "Proj C"})["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _experiment(self) -> str:
        return self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": "exp", "intent": "x"},
        )["id"]

    # ---- orphan-VM sweep ----

    def test_orphan_vm_sweep_reaps_a_running_row_whose_vm_is_gone(self) -> None:
        exp_id = self._experiment()
        sandbox_uid = "uid_gone"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            project_id=self.project_id,
            sandbox_id="sb-gone",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            expires_at="2999-01-01T00:00:00Z",
        )
        # The provider says the VM is gone (never marked alive in the fake).
        self.assertFalse(self.backend.is_alive(sandbox_id="sb-gone"))
        reaped = self.cleanup.sweep_orphan_vms(now=datetime.now(tz=UTC))
        self.assertEqual(reaped, 1)
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=sandbox_uid)
        self.assertEqual(row["status"], "terminated")

    def test_orphan_vm_sweep_leaves_a_live_row_running(self) -> None:
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid="uid_live",
            project_id=self.project_id,
            sandbox_id="sb-live",
            status="running",
            ssh_host="h",
            ssh_port=22,
            ssh_user="root",
            expires_at="2999-01-01T00:00:00Z",
        )
        self.backend.alive["sb-live"] = True
        reaped = self.cleanup.sweep_orphan_vms(now=datetime.now(tz=UTC))
        self.assertEqual(reaped, 0)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "running")

    # ---- blob TTL GC ----

    def test_blob_ttl_gc_deletes_expired_blobs(self) -> None:
        ns = self.project_id
        live = self.app.blobs.put(
            namespace=ns, data=b"keep", expires_at="2999-01-01T00:00:00Z"
        )
        dead = self.app.blobs.put(
            namespace=ns, data=b"drop", expires_at="2000-01-01T00:00:00Z"
        )
        swept = self.cleanup.sweep_expired_blobs(now=datetime.now(tz=UTC))
        self.assertEqual(swept, 1)
        self.assertIsNotNone(self.app.blobs.stat(namespace=ns, sha256=live))
        self.assertIsNone(self.app.blobs.stat(namespace=ns, sha256=dead))

    # ---- stale provisioning reap ----

    def test_stale_provision_reaped_past_deadline(self) -> None:
        exp_id = self._experiment()
        sandbox_uid = "uid_wedged"
        started = "2026-01-01T00:00:00Z"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            project_id=self.project_id,
            sandbox_id="sb-wedged",
            status="provisioning",
            phase="connecting",
            provision_started_at=started,
        )
        self.backend.alive["sb-wedged"] = True
        self.backend.by_experiment[exp_id] = "sb-wedged"
        # 20 minutes later, well past the stale-provision deadline.
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        reaped = self.cleanup.sweep_stale_provisions(now=now)
        self.assertEqual(reaped, 1)
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=sandbox_uid)
        self.assertEqual(row["status"], "failed")
        # The billing VM was terminated by cleanup_orphan.
        self.assertIn("sb-wedged", self.backend.terminated)

    def test_stale_provision_reaped_in_earlier_phase(self) -> None:
        # A daemon crash during `connecting` (Lambda waiting for boot + SSH)
        # leaves a billing VM in a provisioning phase. The sweep must still
        # reap it — the VM exists from `creating` onward.
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid="uid_connecting",
            project_id=self.project_id,
            sandbox_id="sb-connecting",
            status="provisioning",
            phase="connecting",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        self.backend.alive["sb-connecting"] = True
        self.backend.by_experiment[exp_id] = "sb-connecting"
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        reaped = self.cleanup.sweep_stale_provisions(now=now)
        self.assertEqual(reaped, 1)
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid="uid_connecting")
        self.assertEqual(row["status"], "failed")
        self.assertIn("sb-connecting", self.backend.terminated)

    def test_stale_provision_reaped_before_id_recorded(self) -> None:
        # Crash in the narrow window after the provider created the VM but
        # before on_created persisted its id: the row has an empty sandbox_id,
        # so the reap can only find the VM by its deterministic name
        # (cleanup_orphan -> backend.find_sandbox_id). It must still be killed.
        exp_id = self._experiment()
        sandbox_uid = "uid_unrecorded"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            project_id=self.project_id,
            sandbox_id="",
            status="provisioning",
            phase="creating",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        # Only the deterministic-name lookup knows about this VM.
        self.backend.alive["sb-unrecorded"] = True
        self.backend.by_experiment[exp_id] = "sb-unrecorded"
        now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        reaped = self.cleanup.sweep_stale_provisions(now=now)
        self.assertEqual(reaped, 1)
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=sandbox_uid)
        self.assertEqual(row["status"], "failed")
        self.assertIn("sb-unrecorded", self.backend.terminated)

    def test_stale_unrecorded_orphan_retries_uncertain_termination(self) -> None:
        exp_id = self._experiment()
        sandbox_uid = "uid_unconfirmed"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            project_id=self.project_id,
            status="provisioning",
            phase="creating",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        self.backend.alive["sb-unconfirmed"] = True
        self.backend.by_experiment[sandbox_uid] = "sb-unconfirmed"
        self.backend.terminate = lambda *, sandbox_id: False  # type: ignore[method-assign]
        self.backend.is_alive = (  # type: ignore[method-assign]
            lambda *, sandbox_id: (_ for _ in ()).throw(RuntimeError("provider down"))
        )

        reaped = self.cleanup.sweep_stale_provisions(
            now=datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        )

        self.assertEqual(reaped, 0)
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=sandbox_uid)
        self.assertEqual(row["status"], "provisioning")
        self.assertEqual(row["phase"], "cleanup")

    def test_successful_terminate_still_requires_liveness_confirmation(self) -> None:
        self.backend.terminate = lambda *, sandbox_id: True  # type: ignore[method-assign]
        for alive in (True, RuntimeError("provider down")):
            with self.subTest(alive=alive):
                self.backend.is_alive = (  # type: ignore[method-assign]
                    (lambda *, sandbox_id: alive)
                    if isinstance(alive, bool)
                    else lambda *, sandbox_id: (_ for _ in ()).throw(alive)
                )
                self.assertEqual(
                    self.app.sandboxes.lifecycle.terminate_vm(
                        row={"sandbox_id": "sb-terminating"}
                    ),
                    "maybe_alive",
                )

    def test_stale_unrecorded_orphan_retries_uncertain_lookup(self) -> None:
        exp_id = self._experiment()
        sandbox_uid = "uid_lookup_down"
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid=sandbox_uid,
            project_id=self.project_id,
            status="provisioning",
            phase="creating",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        self.backend.find_sandbox_id = (  # type: ignore[method-assign]
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down"))
        )

        reaped = self.cleanup.sweep_stale_provisions(
            now=datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
        )

        self.assertEqual(reaped, 0)
        row = self.app.sandboxes.registry.get_by_uid(sandbox_uid=sandbox_uid)
        self.assertEqual(row["status"], "provisioning")
        self.assertEqual(row["phase"], "cleanup")

    def test_stale_provision_left_alone_within_deadline(self) -> None:
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid="uid_fresh",
            project_id=self.project_id,
            sandbox_id="sb-fresh",
            status="provisioning",
            phase="connecting",
            provision_started_at="2026-01-01T00:00:00Z",
        )
        # Only 2 minutes in — under the deadline, so it keeps provisioning.
        now = datetime(2026, 1, 1, 0, 2, 0, tzinfo=UTC)
        reaped = self.cleanup.sweep_stale_provisions(now=now)
        self.assertEqual(reaped, 0)
        row = self.app.sandboxes.registry.load_row(experiment_id=exp_id)
        self.assertEqual(row["status"], "provisioning")

    # ---- run_all ----

    def test_run_all_returns_per_sweep_counts_and_is_idempotent(self) -> None:
        # One expired blob + one dead-VM row.
        self.app.blobs.put(
            namespace=self.project_id, data=b"x", expires_at="2000-01-01T00:00:00Z"
        )
        exp_id = self._experiment()
        self.app.sandboxes.registry.upsert(
            experiment_id=exp_id,
            sandbox_uid="uid_dead_run_all",
            project_id=self.project_id,
            sandbox_id="sb-dead",
            status="running",
            expires_at="2999-01-01T00:00:00Z",
        )
        future = datetime.now(tz=UTC) + timedelta(hours=1)
        report = self.cleanup.run_all(now=future)
        self.assertEqual(report.orphan_vms_reaped, 1)
        self.assertEqual(report.blobs_swept, 1)
        # A second pass over the cleaned state changes nothing.
        report2 = self.cleanup.run_all(now=future)
        self.assertEqual(report2.as_dict(), {
            "orphan_vms_reaped": 0,
            "blobs_swept": 0,
            "storage_objects_swept": 0,
            "stale_provisions_reaped": 0,
        })


if __name__ == "__main__":
    unittest.main()
