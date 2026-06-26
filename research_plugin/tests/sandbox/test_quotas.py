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

from datetime import UTC, datetime

from backend.app import ResearchPluginApp
from backend.domain.quota_contract import AdmissionRequest
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.quotas import GLOBAL_SCOPE, QuotaService
from backend.utils import PermissionDeniedError


class QuotaAdmissionTest(unittest.TestCase):
    """check_admission against a store seeded with running rows (unit level)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.store = self.app.store
        self.quotas = QuotaService(store=self.store)
        self.project_id = self.app.call_tool("project.create", {"name": "Proj Q"})["id"]
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
                INSERT INTO sandboxes (
                  sandbox_uid, experiment_id, project_id, status, created_at, updated_at
                )
                VALUES (?, ?, ?, 'running', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (f"uid_{experiment_id}", experiment_id, self.project_id),
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
                INSERT INTO sandboxes (
                  sandbox_uid, experiment_id, project_id, status, created_at, updated_at
                )
                VALUES ('uid_exp_o', 'exp_o', ?, 'running', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
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

    # ---- Phase 9: spend kill-switch + running-total budgets ----

    def _generation(
        self,
        *,
        tenant_id: str,
        price: float,
        started_at: str,
        ended_at: str | None,
    ) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandbox_generations
                  (id, experiment_id, project_id, tenant_id, price_usd_per_hour,
                   started_at, ended_at, created_seq)
                VALUES (?, 'exp_g', ?, ?, ?, ?, ?, 0)
                """,
                (
                    f"sbg_{started_at}",
                    self.project_id,
                    tenant_id,
                    price,
                    started_at,
                    ended_at,
                ),
            )

    def test_tenant_kill_switch_denies_admission(self) -> None:
        self.quotas.set_kill_switch(
            scope="tenant_q", tripped=True, reason="abuse review"
        )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(tenant_id="tenant_q", time_limit_seconds=3600)
            )
        self.assertIn("kill-switch", ctx.exception.message)
        self.assertIn("abuse review", ctx.exception.message)
        # Arming it again restores admission.
        self.quotas.set_kill_switch(scope="tenant_q", tripped=False)
        self.quotas.check_admission(
            request=AdmissionRequest(tenant_id="tenant_q", time_limit_seconds=3600)
        )

    def test_global_kill_switch_denies_every_tenant(self) -> None:
        self.quotas.set_kill_switch(
            scope=GLOBAL_SCOPE, tripped=True, reason="provider outage"
        )
        # Even a tenant with no quota row is halted by the platform breaker.
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(tenant_id="any_tenant", time_limit_seconds=60)
            )
        self.assertIn("platform", ctx.exception.message)

    def test_local_tenant_unaffected_without_kill_switch(self) -> None:
        # No kill-switch rows exist ⇒ local 'local' tenant admits freely.
        self.quotas.check_admission(
            request=AdmissionRequest(tenant_id="local", time_limit_seconds=86400)
        )

    def test_gpu_hour_budget_exhausted_denies(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", gpu_hours_budget=2.0)
        # A closed 3-hour generation already exceeds the 2 GPU-hour budget.
        self._generation(
            tenant_id="tenant_q",
            price=1.0,
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T03:00:00Z",
        )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(tenant_id="tenant_q", time_limit_seconds=60)
            )
        self.assertEqual(ctx.exception.details.get("quota"), "gpu_hours_budget")

    def test_usd_budget_exhausted_denies(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", usd_budget=10.0)
        # 5 hours at $3/hr = $15 spent, over the $10 budget.
        self._generation(
            tenant_id="tenant_q",
            price=3.0,
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T05:00:00Z",
        )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(tenant_id="tenant_q", time_limit_seconds=60)
            )
        self.assertEqual(ctx.exception.details.get("quota"), "usd_budget")

    def test_open_generation_bills_to_now(self) -> None:
        # An open generation started 4h ago bills 4 GPU-hours at "now".
        now = datetime(2026, 1, 1, 4, 0, 0, tzinfo=UTC)
        self._generation(
            tenant_id="tenant_q",
            price=2.0,
            started_at="2026-01-01T00:00:00Z",
            ended_at=None,
        )
        spend = self.quotas.tenant_spend(tenant_id="tenant_q", now=now)
        self.assertAlmostEqual(spend["gpu_hours"], 4.0)
        self.assertAlmostEqual(spend["usd"], 8.0)

    def test_closing_a_generation_freezes_spend(self) -> None:
        # Open generation bills to a far-future now; closing it caps the runtime.
        self._generation(
            tenant_id="tenant_q",
            price=2.0,
            started_at="2026-01-01T00:00:00Z",
            ended_at=None,
        )
        far = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        open_spend = self.quotas.tenant_spend(tenant_id="tenant_q", now=far)
        self.assertGreater(open_spend["gpu_hours"], 9.0)
        # Close it at +1h, then the same far-future read is frozen at 1 GPU-hour.
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE sandbox_generations SET ended_at = ? WHERE tenant_id = ?",
                ("2026-01-01T01:00:00Z", "tenant_q"),
            )
        closed_spend = self.quotas.tenant_spend(tenant_id="tenant_q", now=far)
        self.assertAlmostEqual(closed_spend["gpu_hours"], 1.0)
        self.assertAlmostEqual(closed_spend["usd"], 2.0)


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
        )
        self.store = self.app.store
        self.project_id = self.app.call_tool("project.create", {"name": "Proj P"})["id"]

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
                {
                    "project_id": self.project_id,
                    "experiment_id": exp_id,
                    "confirm_retained": True,
                },
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

    def test_release_closes_the_generation(self) -> None:
        # Termination (here via release) stamps ended_at so spend stops accruing.
        exp_id = self._experiment()
        self.app.call_tool(
            "sandbox.request",
            {
                "project_id": self.project_id,
                "experiment_id": exp_id,
                "instance_type": "gpu_1x_a10",
            },
        )
        self.app.call_tool(
            "sandbox.release",
            {
                "project_id": self.project_id,
                "experiment_id": exp_id,
                "confirm_retained": True,
            },
        )
        conn = self.store.connect()
        try:
            gens = conn.execute(
                "SELECT ended_at FROM sandbox_generations WHERE experiment_id = ?",
                (exp_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(gens), 1)
        self.assertIsNotNone(gens[0]["ended_at"])


if __name__ == "__main__":
    unittest.main()
