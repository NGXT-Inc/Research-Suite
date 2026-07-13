"""Cost governance (cloud plan Phase 7): quota admission + spend recording.

No quota row ⇒ unlimited (local parity). With a quota: concurrent-sandbox,
time_limit, and instance-price ceilings are enforced at the procurement choke
point. Every provisioned generation records its provider price quote in the
sandbox_generations ledger, with the price plumbed through FakeSandboxBackend.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from datetime import UTC, datetime

from tests.support.brain import TestBrain
from backend.ports.quota_admission import AdmissionRequest
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.quotas import GLOBAL_SCOPE, QuotaService
from backend.utils import PermissionDeniedError


class QuotaAdmissionTest(unittest.TestCase):
    """check_admission against a store seeded with running rows (unit level)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.store = self.app.store
        self.quotas = QuotaService(store=self.store)
        self.project_id = self.app.call_tool("project", {"action": "create", "name": "Proj Q"})["id"]
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
                  sandbox_uid, project_id, status, created_at, updated_at
                )
                VALUES (?, ?, 'running', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (f"uid_{experiment_id}", self.project_id),
            )
            conn.execute(
                """
                INSERT INTO sandbox_attachments (
                  sandbox_uid, experiment_id, attached_at, detached_at
                )
                VALUES (?, ?, '2026-01-01T00:00:00Z', NULL)
                """,
                (f"uid_{experiment_id}", experiment_id),
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
        other = self.app.call_tool("project", {"action": "create", "name": "Other"})["id"]
        self._set_tenant(other, "tenant_other")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, project_id, status, created_at, updated_at
                )
                VALUES ('uid_exp_o', ?, 'running', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
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

    def test_lifetime_extension_skips_concurrent_count_but_enforces_time_limit(self) -> None:
        self.quotas.set_quota(
            tenant_id="tenant_q",
            max_concurrent_sandboxes=0,
            max_time_limit_seconds=3600,
        )
        self.quotas.check_lifetime_extension(
            tenant_id="tenant_q",
            total_time_limit_seconds=3600,
        )
        with self.assertRaises(PermissionDeniedError):
            self.quotas.check_lifetime_extension(
                tenant_id="tenant_q",
                total_time_limit_seconds=5400,
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

    def test_price_ceiling_fails_closed_when_price_unknown(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", max_price_usd_per_hour=1.0)
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(
                    tenant_id="tenant_q", time_limit_seconds=3600
                )
            )
        self.assertEqual(ctx.exception.details.get("unresolved"), "price")

    # ---- Phase 9: spend kill-switch + running-total budgets ----

    def _generation(
        self,
        *,
        tenant_id: str,
        price: float,
        gpu_count: int = 1,
        started_at: str,
        ended_at: str | None,
    ) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandbox_generations
                  (id, experiment_id, project_id, tenant_id, gpu_count, price_usd_per_hour,
                   started_at, ended_at, created_seq)
                VALUES (?, 'exp_g', ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    f"sbg_{started_at}",
                    self.project_id,
                    tenant_id,
                    gpu_count,
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

    def test_request_projection_denies_before_spending(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", usd_budget=1.0)
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(
                    tenant_id="tenant_q",
                    time_limit_seconds=3600,
                    price_usd_per_hour=23.92,
                    gpu_count=8,
                )
            )
        self.assertEqual(ctx.exception.details.get("quota"), "usd_budget")

    def test_multi_gpu_projection_denies_before_spending(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", gpu_hours_budget=4.0)
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(
                    tenant_id="tenant_q",
                    time_limit_seconds=3600,
                    gpu_count=8,
                )
            )
        self.assertEqual(ctx.exception.details.get("quota"), "gpu_hours_budget")

    def test_cpu_request_reserves_zero_gpu_hours(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", gpu_hours_budget=0.0)
        self.quotas.check_admission(
            request=AdmissionRequest(
                tenant_id="tenant_q", time_limit_seconds=3600, gpu_count=0
            )
        )

    def test_active_reservations_are_included_in_projection(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", usd_budget=1.0)

        def reserve(conn) -> None:
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, project_id, tenant_id, status, price_usd_per_hour,
                  price_known, gpu_count, time_limit, provision_claim, created_at,
                  updated_at
                ) VALUES (?, ?, ?, 'provisioning', ?, 1, 1, 3600, 'claim', ?, ?)
                """,
                (
                    "uid_reserved",
                    self.project_id,
                    "tenant_q",
                    0.75,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )

        self.quotas.reserve_provisioning(
            request=AdmissionRequest(
                tenant_id="tenant_q",
                time_limit_seconds=3600,
                price_usd_per_hour=0.75,
                gpu_count=1,
                sandbox_uid="uid_reserved",
            ),
            reservation=reserve,
        )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(
                    tenant_id="tenant_q",
                    time_limit_seconds=3600,
                    price_usd_per_hour=0.75,
                    gpu_count=1,
                    sandbox_uid="uid_second",
                )
            )
        self.assertEqual(ctx.exception.details.get("quota"), "usd_budget")

    def test_active_unknown_duration_fails_closed(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", usd_budget=10.0)
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, project_id, tenant_id, status, price_usd_per_hour,
                  price_known, gpu_count, time_limit, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', 1, 1, 0, 0, ?, ?)
                """,
                (
                    "uid_unknown_duration",
                    self.project_id,
                    "tenant_q",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.quotas.check_admission(
                request=AdmissionRequest(
                    tenant_id="tenant_q",
                    time_limit_seconds=3600,
                    price_usd_per_hour=1.0,
                    gpu_count=0,
                )
            )
        self.assertEqual(ctx.exception.details.get("unresolved"), "duration")

    def test_lifetime_extension_replaces_current_reservation(self) -> None:
        self.quotas.set_quota(tenant_id="tenant_q", usd_budget=2.1)
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandboxes (
                  sandbox_uid, project_id, tenant_id, status, price_usd_per_hour,
                  price_known, gpu_count, time_limit, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', 1, 1, 1, 3600, ?, ?, ?)
                """,
                (
                    "uid_extend",
                    self.project_id,
                    "tenant_q",
                    "2999-01-01T01:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
        reserved: list[bool] = []
        self.quotas.check_lifetime_extension(
            tenant_id="tenant_q",
            total_time_limit_seconds=7200,
            price_usd_per_hour=1.0,
            gpu_count=1,
            sandbox_uid="uid_extend",
            remaining_time_limit_seconds=7200,
            reservation=lambda conn: reserved.append(True),
        )
        self.assertEqual(reserved, [True])
        self.quotas.set_quota(tenant_id="tenant_q", usd_budget=1.5)
        with self.assertRaises(PermissionDeniedError):
            self.quotas.check_lifetime_extension(
                tenant_id="tenant_q",
                total_time_limit_seconds=7200,
                price_usd_per_hour=1.0,
                gpu_count=1,
                sandbox_uid="uid_extend",
                remaining_time_limit_seconds=7200,
            )

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


class ProjectSpendTest(unittest.TestCase):
    """project_spend: the UI-grouped reading of the generations ledger."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.store = self.app.store
        self.quotas = QuotaService(store=self.store)
        self.project_id = self.app.call_tool("project", {"action": "create", "name": "Proj S"})["id"]
        self._gen_count = 0

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _generation(
        self,
        *,
        experiment_id: str,
        price: float,
        started_at: str,
        ended_at: str | None,
        instance_type: str = "",
        gpu: str = "",
        project_id: str | None = None,
        seq: int = 0,
    ) -> None:
        self._gen_count += 1
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sandbox_generations
                  (id, experiment_id, project_id, tenant_id, instance_type, gpu,
                   price_usd_per_hour, started_at, ended_at, created_seq)
                VALUES (?, ?, ?, 'local', ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"sbg_{self._gen_count}",
                    experiment_id,
                    project_id or self.project_id,
                    instance_type,
                    gpu,
                    price,
                    started_at,
                    ended_at,
                    seq,
                ),
            )

    def test_totals_and_groupings(self) -> None:
        # exp_a: 2h at $2 (closed) = $4; exp_b: 1h at $4 (closed) = $4 plus an
        # unpriced 3h Modal-style generation.
        self._generation(
            experiment_id="exp_a", price=2.0, instance_type="gpu_1x_a10",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T02:00:00Z",
        )
        self._generation(
            experiment_id="exp_b", price=4.0, instance_type="gpu_1x_h100",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T01:00:00Z",
        )
        self._generation(
            experiment_id="exp_b", price=0.0, gpu="A100",
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T03:00:00Z",
        )
        spend = self.quotas.project_spend(project_id=self.project_id)
        self.assertAlmostEqual(spend["total_usd"], 8.0)
        self.assertAlmostEqual(spend["total_hours"], 6.0)
        self.assertAlmostEqual(spend["unpriced_hours"], 3.0)
        self.assertEqual(spend["generations"], 3)
        self.assertEqual(spend["open_generations"], 0)
        self.assertAlmostEqual(spend["burn_usd_per_hour"], 0.0)
        # Groupings sorted by spend, then hours (the $0 generation ranks last
        # among hardware but pushes exp_b's hours ahead of exp_a's).
        self.assertEqual(
            [e["experiment_id"] for e in spend["by_experiment"]], ["exp_b", "exp_a"]
        )
        self.assertAlmostEqual(spend["by_experiment"][0]["usd"], 4.0)
        self.assertAlmostEqual(spend["by_experiment"][0]["hours"], 4.0)
        self.assertEqual(
            [h["instance_type"] for h in spend["by_hardware"]],
            ["gpu_1x_a10", "gpu_1x_h100", ""],
        )
        self.assertEqual(spend["by_hardware"][2]["gpu"], "A100")

    def test_open_generation_bills_to_now_and_sets_burn(self) -> None:
        now = datetime(2026, 1, 2, 6, 0, 0, tzinfo=UTC)
        self._generation(
            experiment_id="exp_a", price=1.5,
            started_at="2026-01-02T00:00:00Z", ended_at=None,
        )
        spend = self.quotas.project_spend(project_id=self.project_id, now=now)
        self.assertAlmostEqual(spend["total_usd"], 9.0)
        self.assertEqual(spend["open_generations"], 1)
        self.assertAlmostEqual(spend["burn_usd_per_hour"], 1.5)

    def test_daily_apportions_across_midnight(self) -> None:
        # 22:00 → 04:00 at $1/h: 2h on day one, 4h on day two.
        self._generation(
            experiment_id="exp_a", price=1.0,
            started_at="2026-01-01T22:00:00Z", ended_at="2026-01-02T04:00:00Z",
        )
        spend = self.quotas.project_spend(project_id=self.project_id)
        self.assertEqual(
            [(d["date"], round(d["hours"], 6), round(d["usd"], 6)) for d in spend["daily"]],
            [("2026-01-01", 2.0, 2.0), ("2026-01-02", 4.0, 4.0)],
        )

    def test_scoped_to_project(self) -> None:
        other = self.app.call_tool("project", {"action": "create", "name": "Other"})["id"]
        self._generation(
            experiment_id="exp_x", price=5.0, project_id=other,
            started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T01:00:00Z",
        )
        spend = self.quotas.project_spend(project_id=self.project_id)
        self.assertEqual(spend["generations"], 0)
        self.assertAlmostEqual(spend["total_usd"], 0.0)
        self.assertEqual(spend["daily"], [])


class QuotaProvisionRecordingTest(unittest.TestCase):
    """End-to-end: price plumbed through provision, generation row recorded."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # Selection-mode fake so the catalog carries instance prices.
        self.backend = FakeSandboxBackend(
            requires_hardware_selection=True, configurable_resources=False
        )
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.backend,
        )
        self.store = self.app.store
        self.project_id = self.app.call_tool("project", {"action": "create", "name": "Proj P"})["id"]

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
                """
                SELECT s.price_usd_per_hour
                FROM sandboxes s
                JOIN sandbox_attachments a ON a.sandbox_uid = s.sandbox_uid
                WHERE a.experiment_id = ?
                """,
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

    def test_multi_gpu_generation_persists_count_and_bills_gpu_hours(self) -> None:
        exp_id = self._experiment()
        self.app.call_tool(
            "sandbox.request",
            {
                "project_id": self.project_id,
                "experiment_id": exp_id,
                "instance_type": "gpu_8x_h100",
            },
        )
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT gpu_count FROM sandbox_generations WHERE experiment_id = ?",
                (exp_id,),
            ).fetchone()
            conn.execute(
                "UPDATE sandbox_generations SET started_at = ?, ended_at = ? "
                "WHERE experiment_id = ?",
                (
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T02:00:00Z",
                    exp_id,
                ),
            )
        self.assertEqual(int(row["gpu_count"]), 8)
        spend = QuotaService(store=self.store).tenant_spend(
            tenant_id="local", now=datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
        )
        self.assertAlmostEqual(spend["gpu_hours"], 16.0)

    def test_generation_failure_cannot_leave_a_running_unbilled_sandbox(self) -> None:
        exp_id = self._experiment()
        with patch.object(
            self.app.sandboxes.registry,
            "record_generation",
            side_effect=RuntimeError("ledger unavailable"),
        ):
            result = self.app.call_tool(
                "sandbox.request",
                {
                    "project_id": self.project_id,
                    "experiment_id": exp_id,
                    "instance_type": "gpu_1x_a10",
                },
            )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(self.backend.terminated)
        conn = self.store.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM sandbox_generations WHERE experiment_id = ?",
                (exp_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(count["n"]), 0)

    def test_actual_provider_price_is_revalidated_before_running(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE projects SET tenant_id = 'tenant_actual' WHERE id = ?",
                (self.project_id,),
            )
        QuotaService(store=self.store).set_quota(
            tenant_id="tenant_actual", usd_budget=1.0
        )
        original = self.backend.acquire

        def acquire(**kwargs):
            return replace(original(**kwargs), price_usd_per_hour=2.0)

        self.backend.acquire = acquire  # type: ignore[method-assign]
        result = self.app.call_tool(
            "sandbox.request",
            {
                "project_id": self.project_id,
                "experiment_id": self._experiment(),
                "instance_type": "gpu_1x_a10",
            },
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(self.backend.terminated)
        conn = self.store.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM sandbox_generations"
            ).fetchone()
            reserved = conn.execute(
                "SELECT price_usd_per_hour, price_known FROM sandboxes"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(count["n"]), 0)
        self.assertEqual(
            (float(reserved["price_usd_per_hour"]), int(reserved["price_known"])),
            (2.0, 1),
        )

    def test_parallel_requests_cannot_overbook_concurrency(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE projects SET tenant_id = 'tenant_race' WHERE id = ?",
                (self.project_id,),
            )
        QuotaService(store=self.store).set_quota(
            tenant_id="tenant_race", max_concurrent_sandboxes=1
        )
        self.app.sandboxes.request_wait_seconds = 0.05
        self.backend.gate = threading.Event()
        barrier = threading.Barrier(2)

        def request() -> dict | PermissionDeniedError:
            barrier.wait()
            try:
                return self.app.sandboxes.request(
                    project_id=self.project_id,
                    public_key="ssh-ed25519 AAAA race@test",
                    instance_type="gpu_1x_a10",
                    include_data_plane_enrichment=False,
                )
            except PermissionDeniedError as exc:
                return exc

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: request(), range(2)))
        finally:
            self.backend.gate.set()

        admitted = [result for result in results if isinstance(result, dict)]
        denied = [
            result for result in results if isinstance(result, PermissionDeniedError)
        ]
        self.assertEqual(len(admitted), 1, results)
        self.assertEqual(admitted[0]["status"], "provisioning")
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].details.get("quota"), "max_concurrent_sandboxes")

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
