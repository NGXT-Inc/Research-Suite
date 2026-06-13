"""Local→cloud import (cloud plan Phase 8).

sqlite→sqlite is fine for the test — the tenant scoping is what matters. Drives
a real local app through a slice of the loop, imports into a fresh
tenant-scoped store, and asserts: entity ids carry over, the project is
re-tenanted, events are re-keyed order-preserving, gated blobs backfill only
where the working-tree file still matches the pin, preconditions block, and the
one-way tombstone lands.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.import_tool import import_local_to_cloud, is_tombstoned
from backend.state import StateStore
from backend.state.blobs import LocalDirBlobStore
from backend.utils import ValidationError
from tests.fakes import FakeRsyncSyncer

VALID_PLAN = (
    "## Summary\nImport test plan.\n\n"
    "## Objective & hypothesis\nThreshold rule beats majority.\n\n"
    "## Evaluation\nMetric: accuracy; success if > 0.6.\n"
)


class ImportToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.local_db = self.repo / ".research_plugin" / "state.sqlite"
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.local_db,
            execution_backend=FakeSandboxBackend(),
            rsync_syncer=FakeRsyncSyncer(),
        )
        self.project_id = self.app.call_tool("project.create", {"name": "Local P"})["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _fresh_cloud(self):
        cloud_db = self.repo / "cloud.sqlite"
        store = StateStore(db_path=cloud_db)
        blobs = LocalDirBlobStore(root=self.repo / "cloud_blobs")
        return store, blobs

    def _seed_claim_and_experiment_with_plan(self) -> tuple[str, str]:
        claim = self.app.call_tool(
            "claim.create",
            {"project_id": self.project_id, "statement": "A claim to import."},
        )
        exp = self.app.call_tool(
            "experiment.create",
            {"project_id": self.project_id, "name": "imp", "intent": "import me",
             "tested_claim_ids": [claim["id"]]},
        )
        (self.repo / "experiments" / "imp").mkdir(parents=True, exist_ok=True)
        (self.repo / "experiments" / "imp" / "plan.md").write_text(VALID_PLAN)
        res = self.app.call_tool(
            "resource.register_file",
            {"project_id": self.project_id, "path": "experiments/imp/plan.md", "kind": "plan"},
        )
        self.app.call_tool(
            "resource.associate",
            {"project_id": self.project_id, "resource_id": res["id"],
             "target_type": "experiment", "target_id": exp["id"], "role": "plan"},
        )
        return claim["id"], exp["id"]

    def test_import_carries_ids_and_retenants_into_the_cloud_store(self) -> None:
        claim_id, exp_id = self._seed_claim_and_experiment_with_plan()
        self.app.shutdown()  # release the local store before reading it cold

        store, blobs = self._fresh_cloud()
        summary = import_local_to_cloud(
            local_db_path=self.local_db,
            repo_root=self.repo,
            target_store=store,
            tenant_id="acme",
            target_blobs=blobs,
        )
        # The local store also has the default-bootstrap project, so >=1.
        self.assertGreaterEqual(summary["projects"], 1)
        self.assertGreaterEqual(summary["claims"], 1)
        self.assertGreaterEqual(summary["experiments"], 1)
        # Gated plan bytes backfilled (the working-tree file still matches).
        self.assertEqual(summary["blobs_backfilled"], 1)

        conn = store.connect()
        try:
            project = conn.execute(
                "SELECT id, tenant_id FROM projects WHERE id = ?", (self.project_id,)
            ).fetchone()
            self.assertIsNotNone(project)
            self.assertEqual(project["tenant_id"], "acme")  # re-tenanted
            claim = conn.execute(
                "SELECT id FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
            self.assertIsNotNone(claim)  # id carried over
            exp = conn.execute(
                "SELECT id FROM experiments WHERE id = ?", (exp_id,)
            ).fetchone()
            self.assertIsNotNone(exp)
            # Events imported and re-keyed with fresh ordered ids.
            events = conn.execute("SELECT id FROM events ORDER BY id").fetchall()
            self.assertTrue(events)
            ids = [e["id"] for e in events]
            self.assertEqual(ids, sorted(ids))
        finally:
            conn.close()
        # The gated plan blob is readable in the cloud blob store.
        version = self._cloud_plan_version(store)
        self.assertIsNotNone(
            blobs.stat(namespace=self.project_id, sha256=version)
        )

    def _cloud_plan_version(self, store) -> str:
        conn = store.connect()
        try:
            row = conn.execute(
                "SELECT content_sha256 FROM resource_versions LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return str(row["content_sha256"])

    def test_drifted_working_tree_file_imports_metadata_only(self) -> None:
        self._seed_claim_and_experiment_with_plan()
        # Drift the working-tree file away from the pinned sha.
        (self.repo / "experiments" / "imp" / "plan.md").write_text("DRIFTED\n")
        self.app.shutdown()

        store, blobs = self._fresh_cloud()
        summary = import_local_to_cloud(
            local_db_path=self.local_db,
            repo_root=self.repo,
            target_store=store,
            tenant_id="acme",
            target_blobs=blobs,
        )
        # Metadata imported, but no blob backfilled (the file no longer matches).
        self.assertEqual(summary["blobs_backfilled"], 0)

    def test_open_review_blocks_import(self) -> None:
        _claim, exp_id = self._seed_claim_and_experiment_with_plan()
        self.app.call_tool(
            "experiment.transition",
            {"project_id": self.project_id, "experiment_id": exp_id, "transition": "submit_design"},
        )
        self.app.call_tool(
            "review.request",
            {"project_id": self.project_id, "target_type": "experiment",
             "target_id": exp_id, "role": "design_reviewer"},
        )
        self.app.shutdown()
        store, blobs = self._fresh_cloud()
        with self.assertRaises(ValidationError) as ctx:
            import_local_to_cloud(
                local_db_path=self.local_db, repo_root=self.repo,
                target_store=store, tenant_id="acme", target_blobs=blobs,
            )
        self.assertIn("open review", ctx.exception.message)

    def test_import_writes_one_way_tombstone_and_refuses_reimport(self) -> None:
        self._seed_claim_and_experiment_with_plan()
        self.app.shutdown()
        store, blobs = self._fresh_cloud()
        import_local_to_cloud(
            local_db_path=self.local_db, repo_root=self.repo,
            target_store=store, tenant_id="acme", target_blobs=blobs,
        )
        self.assertTrue(is_tombstoned(self.local_db))
        # A second import is refused — the flip is one-way.
        store2, blobs2 = StateStore(db_path=self.repo / "cloud2.sqlite"), LocalDirBlobStore(
            root=self.repo / "cloud_blobs2"
        )
        with self.assertRaises(ValidationError) as ctx:
            import_local_to_cloud(
                local_db_path=self.local_db, repo_root=self.repo,
                target_store=store2, tenant_id="acme", target_blobs=blobs2,
            )
        self.assertIn("already imported", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
