"""Cost governance (cloud plan Phase 7): quota admission + spend recording.

No quota row ⇒ unlimited (local parity). With a quota: concurrent-sandbox,
time_limit, and instance-price ceilings are enforced at the procurement choke
point. Every provisioned generation records its provider price quote in the
sandbox_generations ledger, with the price plumbed through FakeSandboxBackend.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.quotas import AdmissionRequest, QuotaService
from backend.utils import PermissionDeniedError
from tests.fakes import FakeRsyncSyncer


class QuotaAdmissionTest(unittest.TestCase):
    """check_admission against a store seeded with running rows (unit level)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.store = self.app.store
        self.quotas = QuotaService(store=self.store)
        self.project_id = self.app.call_tool("project.create", {"name": "Q"})["id"]
        self._set_tenant(self.project_id, "tenant_q")

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _set_tenant(self, project_id: str, tenant_id: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE projects SET tenant_id = ? WHERE id = ?",
                (tenant_id, project_id),
            )

    def _running_sandbox(self, experiment_id: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandboxes (experiment_id, project_id, status, created_at, updated_at)
                VALUES (?, ?, 'running', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (experiment_id, self.project_id),
            )

    def test_no_quota_row_is_unlimited(self) -> None:
        # Local parity: no quota row ⇒ admission never raises.
        for _ in range(5):
            self.quotas.check_admission(
                request=AdmissionRequest(
                    tenant_id="tenant_q", time_limit_seconds=86400, price_usd_per_hour=999.0
                )
            )

    def test_local_tenant_is_unlimited(self) -> None:
        self.quotas.check_admission(
            request=AdmissionRequest(tenant_id="local", time_limit_seconds=86400)
        )

    def test_max_concurrent_sandboxes_enforced(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", max_concurrent_sandboxes=2)
        self._running_sandbox("exp_1")
        self._running_sandbox("exp_2")
        self.assertEqual(self.quotas.running_sandbox_count(tenant_id="tenant_q"), 2)
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(tenant_id="tenant_q", time_limit_seconds=3600)
            )
        self.assertIn("quota", ctx.exception.message)

    def test_concurrent_count_is_tenant_scoped(self) -> None:
        # Another tenant's running sandboxes don't count against this tenant.
        other = self.app.call_tool("project.create", {"name": "Other"})["id"]
        self._set_tenant(other, "tenant_other")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandboxes (experiment_id, project_id, status, created_at, updated_at)
                VALUES ('exp_o', ?, 'running', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (other,),
            )
        self.assertEqual(self.quotas.running_sandbox_count(tenant_id="tenant_q"), 0)

    def test_max_time_limit_enforced(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", max_time_limit_seconds=3600)
        # Under the ceiling: fine.
        self.quotas.check_admission(
            request=AdmissionRequest(tenant_id="tenant_q", time_limit_seconds=3600)
        )
        with self.assertRaises(PermissionDeniedError):
            self.quotas.check_admission(
                request=AdmissionRequest(tenant_id="tenant_q", time_limit_seconds=7200)
            )

    def test_max_price_enforced(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", max_price_usd_per_hour=1.0)
        self.quotas.check_admission(
            request=AdmissionRequest(
                tenant_id="tenant_q", time_limit_seconds=3600, price_usd_per_hour=0.75
            )
        )
        with self.assertRaises(PermissionDeniedError):
            self.quotas.check_admission(
                request=AdmissionRequest(
                    tenant_id="tenant_q", time_limit_seconds=3600, price_usd_per_hour=2.5
                )
            )

    def test_price_ceiling_skipped_when_price_unknown(self) -> None:
        # Modal-like: no per-hour quote ⇒ the price gate doesn't bite.
        self.quotas.set_quota(tenant_id="tenant_q", max_price_usd_per_hour=1.0)
        self.quotas.check_admission(
            request=AdmissionRequest(
                tenant_id="tenant_q", time_limit_seconds=3600, price_usd_per_hour=None
            )
        )


class QuotaProvisionRecordingTest(unittest.TestCase):
    """End-to-end: price plumbed through provision, generation row recorded."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # Selection-mode fake so the catalog carries instance prices.
        self.backend = FakeSandboxBackend(
            requires_hardware_selection=True, configurable_resources=False
        )
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.store = self.app.store
        self.project_id = self.app.call_tool("project.create", {"name": "P"})["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _experiment(self) -> str:
        exp_id = self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": "exp-1", "intent": "x"},
        )["id"]
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'ready_to_run' WHERE id = ?",
                (exp_id,),
            )
        return exp_id

    def test_price_recorded_on_row_and_generation_ledger(self) -> None:
        exp_id = self._experiment()
        # gpu_1x_a100 is priced 1.29 in the fake's default catalog.
        result = self.app.call_tool(
            "sandbox.request",
            {
                "project_id": self.project_id,
                "experiment_id": exp_id,
                "instance_type": "gpu_1x_a100",
            },
        )
        self.assertEqual(result["status"], "running")
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT price_usd_per_hour FROM sandboxes WHERE experiment_id = ?",
                (exp_id,),
            ).fetchone()
            gens = conn.execute(
                "SELECT instance_type, price_usd_per_hour, tenant_id "
                "FROM sandbox_generations WHERE experiment_id = ? ORDER BY created_seq",
                (exp_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertAlmostEqual(float(row["price_usd_per_hour"]), 1.29)
        self.assertEqual(len(gens), 1)
        self.assertEqual(gens[0]["instance_type"], "gpu_1x_a100")
        self.assertAlmostEqual(float(gens[0]["price_usd_per_hour"]), 1.29)
        # Local mode: the generation row carries the implicit 'local' tenant.
        self.assertEqual(gens[0]["tenant_id"], "local")

    def test_each_provision_appends_a_generation_row(self) -> None:
        exp_id = self._experiment()
        for _ in range(2):
            self.app.call_tool(
                "sandbox.request",
                {
                    "project_id": self.project_id,
                    "experiment_id": exp_id,
                    "instance_type": "gpu_1x_a10",
                },
            )
            # Release so the next request re-provisions instead of reusing.
            self.app.call_tool(
                "sandbox.release",
                {"project_id": self.project_id, "experiment_id": exp_id},
            )
        conn = self.store.connect()
        try:
            gens = conn.execute(
                "SELECT id FROM sandbox_generations WHERE experiment_id = ?",
                (exp_id,),
            ).fetchall()
        finally:
            conn.close()
        # Two fresh provisions ⇒ two ledger rows (the row itself was overwritten).
        self.assertEqual(len(gens), 2)


if __name__ == "__main__":
    unittest.main()
