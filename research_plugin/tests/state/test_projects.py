from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.services.projects import ProjectService
from backend.state.store import StateStore
from backend.utils import now_iso


class ProjectServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(db_path=Path(self.tmp.name) / "state.sqlite")
        self.projects = ProjectService(store=self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_project_ids_for_tenant_matches_tenant_exactly(self) -> None:
        now = now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, summary, tenant_id, created_at)
                VALUES (?, ?, ?, ?, ?), (?, ?, ?, ?, ?)
                """,
                (
                    "proj_plain",
                    "Plain",
                    "",
                    "acme",
                    now,
                    "proj_spaced",
                    "Spaced",
                    "",
                    " acme ",
                    now,
                ),
            )

        self.assertEqual(
            self.projects.project_ids_for_tenant(tenant_id="acme"), {"proj_plain"}
        )
        self.assertEqual(
            self.projects.project_ids_for_tenant(tenant_id=" acme "), {"proj_spaced"}
        )


if __name__ == "__main__":
    unittest.main()
