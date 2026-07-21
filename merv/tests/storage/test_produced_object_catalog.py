from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from merv.brain.application.ports.storage import ProducedObjectCatalog
from merv.brain.kernel.state.store import StateStore
from merv.brain.object_storage.catalog import StorageObjectCatalog


class CountingStateStore(StateStore):
    def __init__(self, *, db_path: Path) -> None:
        self.statements: list[str] = []
        super().__init__(db_path=db_path)

    def connect(self):
        conn = super().connect()
        conn.set_trace_callback(self.statements.append)
        return conn


class ProducedObjectCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CountingStateStore(
            db_path=Path(self.tmp.name) / "state.sqlite"
        )
        with closing(self.store.connect()) as conn:
            project = conn.execute("SELECT id FROM projects ORDER BY created_at LIMIT 1").fetchone()
            assert project is not None
            self.project_id = str(project["id"])
        self.catalog = StorageObjectCatalog(store=self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _insert(
        self,
        *,
        object_id: str,
        experiment_id: str,
        name: str,
        version: int,
        kind: str,
        status: str = "available",
        created_seq: int,
        project_id: str | None = None,
    ) -> None:
        owner_project_id = project_id or self.project_id
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO storage_objects (
                  id, project_id, name, version, kind, content_sha256,
                  size_bytes, content_type, namespace, status, upload_id,
                  expires_at, created_by, producing_experiment_id,
                  producing_run, source_uri, notes, created_at, updated_at,
                  last_accessed_at, created_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'codex', ?,
                          ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    object_id,
                    owner_project_id,
                    name,
                    version,
                    kind,
                    object_id.removeprefix("so_").ljust(64, "a")[:64],
                    version,
                    "application/octet-stream",
                    owner_project_id,
                    status,
                    None,
                    experiment_id,
                    f"run-{experiment_id}",
                    "",
                    f"note-{object_id}",
                    f"2026-07-21T00:00:0{created_seq}Z",
                    f"2026-07-21T00:00:0{created_seq}Z",
                    None,
                    created_seq,
                ),
            )

    def test_catalog_isolates_identical_experiment_ids_by_project(self) -> None:
        other_project_id = "proj_other"
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, summary, created_at) "
                "VALUES (?, ?, '', ?)",
                (other_project_id, "Other project", "2026-07-21T00:00:00Z"),
            )
        self._insert(
            object_id="so_local",
            experiment_id="exp_shared",
            name="local.bin",
            version=1,
            kind="other",
            created_seq=1,
        )
        self._insert(
            object_id="so_other",
            experiment_id="exp_shared",
            name="other.bin",
            version=1,
            kind="other",
            created_seq=2,
            project_id=other_project_id,
        )

        local = self.catalog.by_experiment(
            project_id=self.project_id,
            experiment_ids=("exp_shared",),
        )
        other = self.catalog.by_experiment(
            project_id=other_project_id,
            experiment_ids=("exp_shared",),
        )

        self.assertEqual(
            [item["id"] for item in local["exp_shared"]],
            ["so_local"],
        )
        self.assertEqual(
            [item["id"] for item in other["exp_shared"]],
            ["so_other"],
        )

    def test_structurally_implements_application_catalog_without_provider(self) -> None:
        self.assertIsInstance(self.catalog, ProducedObjectCatalog)

    def test_batches_historical_rows_with_exact_filter_fields_and_order(self) -> None:
        self._insert(
            object_id="so_old",
            experiment_id="exp_1",
            name="models/checkpoint.bin",
            version=1,
            kind="model",
            created_seq=1,
        )
        self._insert(
            object_id="so_new",
            experiment_id="exp_1",
            name="models/checkpoint.bin",
            version=2,
            kind="model",
            status="expired",
            created_seq=2,
        )
        self._insert(
            object_id="so_uploading",
            experiment_id="exp_2",
            name="datasets/train.tar",
            version=1,
            kind="dataset",
            status="uploading",
            created_seq=3,
        )
        self._insert(
            object_id="so_deleted",
            experiment_id="exp_2",
            name="deleted.bin",
            version=1,
            kind="other",
            status="deleted",
            created_seq=4,
        )
        self.store.statements.clear()

        result = self.catalog.by_experiment(
            project_id=self.project_id,
            experiment_ids=("exp_1", "exp_2", "exp_missing"),
        )

        self.assertEqual([item["id"] for item in result["exp_1"]], ["so_new", "so_old"])
        self.assertEqual([item["id"] for item in result["exp_2"]], ["so_uploading"])
        self.assertEqual(result["exp_missing"], [])
        self.assertEqual(
            list(result["exp_1"][0]),
            [
                "id", "name", "version", "kind", "content_sha256",
                "size_bytes", "content_type", "status", "expires_at",
                "producing_run", "source_uri", "notes", "created_at",
                "updated_at", "last_accessed_at",
            ],
        )
        self.assertNotIn("namespace", result["exp_1"][0])
        self.assertNotIn("producing_experiment_id", result["exp_1"][0])
        storage_reads = [
            statement
            for statement in self.store.statements
            if "FROM storage_objects" in statement
        ]
        self.assertEqual(len(storage_reads), 1)

    def test_one_and_twenty_five_ids_each_use_one_storage_query(self) -> None:
        experiment_ids = tuple(f"exp_{index}" for index in range(1, 26))
        for index, experiment_id in enumerate(experiment_ids, start=1):
            self._insert(
                object_id=f"so_{index}",
                experiment_id=experiment_id,
                name=f"object-{index}",
                version=1,
                kind="other",
                created_seq=index,
            )
        for ids in (("exp_1",), experiment_ids):
            with self.subTest(ids=ids):
                self.store.statements.clear()
                self.catalog.by_experiment(
                    project_id=self.project_id, experiment_ids=ids
                )
                self.assertEqual(
                    sum(
                        "FROM storage_objects" in statement
                        for statement in self.store.statements
                    ),
                    1,
                )

    def test_large_batch_is_chunked_below_sql_parameter_limits(self) -> None:
        experiment_ids = tuple(f"exp_{index}" for index in range(801))
        self.store.statements.clear()

        result = self.catalog.by_experiment(
            project_id=self.project_id, experiment_ids=experiment_ids
        )

        self.assertEqual(list(result), list(experiment_ids))
        self.assertEqual(
            sum(
                "FROM storage_objects" in statement
                for statement in self.store.statements
            ),
            3,
        )

    def test_empty_batch_opens_no_connection(self) -> None:
        self.store.statements.clear()

        self.assertEqual(
            self.catalog.by_experiment(
                project_id=self.project_id, experiment_ids=()
            ),
            {},
        )
        self.assertEqual(self.store.statements, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
