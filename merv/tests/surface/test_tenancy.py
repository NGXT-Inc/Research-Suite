"""Tenancy enforcement + capability hardening (cloud plan Phase 7).

Two tenants share one store. Cross-tenant project access is denied at the
record layer (require_project_id with a tenant scope); a project created under
tenant A is invisible to tenant B; the reviewer capability round-trips through
its hash (mint returns plaintext, start resolves by hash); and a review session
started by the wrong tenant is rejected.

Local mode never threads a tenant, so all of this is dormant under the single
'local' tenant — these tests exercise the control-mode primitives directly at
the service/store layer (the plan's sanctioned scope for Phase 7).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.brain import TestBrain
from backend.execution.backends.fake import FakeSandboxBackend
from backend.utils import NotFoundError, PermissionDeniedError


class TenancyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )
        self.store = self.app.store
        # Two projects, re-homed to two distinct tenants (bootstrap default is
        # the 'local' tenant; control mode would create them under a tenant).
        self.proj_a = self.app.call_tool("project", {"action": "create", "name": "Proj A"})["id"]
        self.proj_b = self.app.call_tool("project", {"action": "create", "name": "Proj B"})["id"]
        self._set_tenant(self.proj_a, "tenant_a")
        self._set_tenant(self.proj_b, "tenant_b")

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _set_tenant(self, project_id: str, tenant_id: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE projects SET tenant_id = ? WHERE id = ?",
                (tenant_id, project_id),
            )

    # ---- require_project_id tenant scoping ----

    def test_require_project_id_default_is_unscoped(self) -> None:
        # No tenant ⇒ today's behavior: any existing project resolves.
        conn = self.store.connect()
        try:
            self.assertEqual(
                self.store.require_project_id(conn=conn, project_id=self.proj_a),
                self.proj_a,
            )
        finally:
            conn.close()

    def test_own_tenant_resolves(self) -> None:
        conn = self.store.connect()
        try:
            got = self.store.require_project_id(
                conn=conn, project_id=self.proj_a, tenant_id="tenant_a"
            )
            self.assertEqual(got, self.proj_a)
        finally:
            conn.close()

    def test_cross_tenant_project_reads_as_not_found(self) -> None:
        conn = self.store.connect()
        try:
            with self.assertRaises(NotFoundError):
                self.store.require_project_id(
                    conn=conn, project_id=self.proj_a, tenant_id="tenant_b"
                )
        finally:
            conn.close()

    def test_project_created_under_tenant_a_invisible_to_tenant_b(self) -> None:
        conn = self.store.connect()
        try:
            # B can see its own project but not A's.
            self.assertEqual(
                self.store.require_project_id(
                    conn=conn, project_id=self.proj_b, tenant_id="tenant_b"
                ),
                self.proj_b,
            )
            with self.assertRaises(NotFoundError):
                self.store.require_project_id(
                    conn=conn, project_id=self.proj_b, tenant_id="tenant_a"
                )
        finally:
            conn.close()

    # ---- capability_hash round trip ----

    def _requestable_experiment(self, project_id: str) -> str:
        exp_id = self.app.call_tool(
            "experiment.create",
            {"project_id": project_id, "name": "exp-x", "intent": "test it"},
        )["id"]
        # Put the experiment in a design review gate so review.request accepts a
        # design_reviewer role.
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE experiments SET status = 'design_review' WHERE id = ?",
                (exp_id,),
            )
        return exp_id

    def test_capability_is_stored_hashed_and_resolves_by_hash(self) -> None:
        import hashlib

        exp_id = self._requestable_experiment(self.proj_a)
        req = self.app.reviews.request(
            target_type="experiment",
            target_id=exp_id,
            role="design_reviewer",
            project_id=self.proj_a,
        )
        capability = req["reviewer_capability"]
        self.assertTrue(capability.startswith("rp_"))
        # At rest only the hash exists; the plaintext is nowhere in the row.
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT capability_hash FROM review_requests WHERE id = ?",
                (req["review_request_id"],),
            ).fetchone()
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(review_requests)").fetchall()
            }
        finally:
            conn.close()
        self.assertNotIn("capability", cols)
        self.assertEqual(
            row["capability_hash"],
            hashlib.sha256(capability.encode("utf-8")).hexdigest(),
        )
        # start resolves by hashing the presented plaintext.
        started = self.app.reviews.start(
            review_request_id=req["review_request_id"],
            reviewer_capability=capability,
            caller_session_id="reviewer",
        )
        self.assertTrue(started["review_session_id"])

    def test_wrong_capability_rejected(self) -> None:
        exp_id = self._requestable_experiment(self.proj_a)
        req = self.app.reviews.request(
            target_type="experiment",
            target_id=exp_id,
            role="design_reviewer",
            project_id=self.proj_a,
        )
        with self.assertRaises(PermissionDeniedError):
            self.app.reviews.start(
                review_request_id=req["review_request_id"],
                reviewer_capability="rp_wrong",
                caller_session_id="reviewer",
            )

    # ---- review session bound to wrong tenant rejected ----

    def test_review_start_rejects_foreign_tenant(self) -> None:
        exp_id = self._requestable_experiment(self.proj_a)
        req = self.app.reviews.request(
            target_type="experiment",
            target_id=exp_id,
            role="design_reviewer",
            project_id=self.proj_a,
        )
        # A reviewer authenticated to tenant_b cannot start a review against
        # tenant_a's target — it reads as not-found (no existence leak).
        with self.assertRaises(NotFoundError):
            self.app.reviews.start(
                review_request_id=req["review_request_id"],
                reviewer_capability=req["reviewer_capability"],
                caller_session_id="reviewer",
                tenant_id="tenant_b",
            )
        # The owning tenant can start it, and the session records that tenant.
        started = self.app.reviews.start(
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id="reviewer",
            tenant_id="tenant_a",
        )
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT tenant_id FROM review_sessions WHERE id = ?",
                (started["review_session_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["tenant_id"], "tenant_a")

    def test_review_start_local_mode_no_op_binds_local_tenant(self) -> None:
        # No tenant threaded (local mode): the session binds the 'local' tenant
        # and behaves exactly as before.
        exp_id = self._requestable_experiment(self.proj_a)
        req = self.app.reviews.request(
            target_type="experiment",
            target_id=exp_id,
            role="design_reviewer",
            project_id=self.proj_a,
        )
        started = self.app.reviews.start(
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id="reviewer",
        )
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT tenant_id FROM review_sessions WHERE id = ?",
                (started["review_session_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["tenant_id"], "local")


if __name__ == "__main__":
    unittest.main()
