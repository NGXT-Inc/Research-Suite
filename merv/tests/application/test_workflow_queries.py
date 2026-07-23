from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from merv.brain.kernel.state.store import StateStore
from tests.support.brain import TestBrain


class CountingStateStore(StateStore):
    def __init__(self, *, db_path: Path) -> None:
        self.statements: list[str] = []
        super().__init__(db_path=db_path)

    def connect(self):
        conn = super().connect()
        conn.set_trace_callback(self.statements.append)
        return conn


class StatusAndNextQueryIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=root,
            db_path=root / ".research_plugin" / "state.sqlite",
        )
        self.project_id = self.app.call_tool(
            "project", {"action": "create", "name": "Workflow query"}
        )["id"]
        self.experiment_ids = [
            self.app.call_tool(
                "experiment.create",
                {
                    "project_id": self.project_id,
                    "name": f"read-{index}",
                    "intent": f"Read experiment {index} once.",
                },
            )["id"]
            for index in range(2)
        ]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def test_project_dashboard_uses_only_plural_experiment_hydration(self) -> None:
        original_singular = self.app.experiments.get_state_with_gate
        original_batch = self.app.experiments.list_states_with_gates
        batch = Mock(wraps=original_batch)
        singular = Mock(
            side_effect=AssertionError("dashboard used singular state read")
        )
        singular_evidence = Mock(
            side_effect=AssertionError("dashboard used singular evidence read")
        )
        self.app.experiments.list_states_with_gates = batch
        self.app.experiments.get_state_with_gate = singular
        original_evidence = self.app.resources.resources_for_target
        self.app.resources.resources_for_target = singular_evidence
        try:
            result = self.app.project_dashboard_query(project_id=self.project_id)
        finally:
            self.app.experiments.list_states_with_gates = original_batch
            self.app.experiments.get_state_with_gate = original_singular
            self.app.resources.resources_for_target = original_evidence

        self.assertCountEqual(
            [experiment["id"] for experiment in result["experiments"]],
            self.experiment_ids,
        )
        batch.assert_called_once()
        self.assertEqual(batch.call_args.kwargs["project_id"], self.project_id)
        singular.assert_not_called()
        singular_evidence.assert_not_called()

    def test_scoped_workflow_hydrates_only_the_selected_experiment(self) -> None:
        original = self.app.experiments.get_state_with_gate
        wrapped = Mock(wraps=original)
        batch = Mock(
            side_effect=AssertionError("selected-only workflow used plural state read")
        )
        self.app.experiments.get_state_with_gate = wrapped
        original_batch = self.app.experiments.list_states_with_gates
        self.app.experiments.list_states_with_gates = batch
        try:
            result = self.app.workflow.status_and_next(
                project_id=self.project_id,
                experiment_id=self.experiment_ids[0],
            )
        finally:
            self.app.experiments.get_state_with_gate = original
            self.app.experiments.list_states_with_gates = original_batch

        self.assertEqual(result["experiment"]["id"], self.experiment_ids[0])
        self.assertEqual(
            [str(call.kwargs["experiment_id"]) for call in wrapped.call_args_list],
            [self.experiment_ids[0]],
        )
        batch.assert_not_called()

    def test_sandbox_reads_enforce_project_scope(self) -> None:
        other_project = self.app.call_tool(
            "project", {"action": "create", "name": "Other project"}
        )["id"]
        other_experiment = self.app.call_tool(
            "experiment.create",
            {
                "project_id": other_project,
                "name": "other-read",
                "intent": "Remain private to the other project.",
            },
        )["id"]
        self.app.sandbox_runtime.repository.upsert(
            experiment_id=other_experiment,
            sandbox_uid="sb_other_project",
            project_id=other_project,
            sandbox_id="provider-other",
            status="ready",
        )

        self.assertEqual(
            len(
                self.app.sandboxes.for_experiment(
                    project_id=other_project, experiment_id=other_experiment
                )
            ),
            1,
        )
        self.assertEqual(
            self.app.sandboxes.for_experiment(
                project_id=self.project_id, experiment_id=other_experiment
            ),
            [],
        )


class ProjectDashboardBatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = CountingStateStore(db_path=root / "state.sqlite")
        self.app = TestBrain(
            repo_root=root,
            db_path=root / "state.sqlite",
            store=self.store,
        )

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _seed_project(
        self,
        *,
        project_id: str,
        active: int,
        terminal: int,
        detailed: bool = False,
    ) -> list[str]:
        experiment_ids: list[str] = []
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                (project_id, project_id, "2026-07-22T00:00:00Z"),
            )
            for index in range(active + terminal):
                experiment_id = f"exp_{project_id}_{index:03d}"
                experiment_ids.append(experiment_id)
                created_at = f"2026-07-22T00:{index:02d}:00Z"
                status = "planned" if index < active else "complete"
                conn.execute(
                    """
                    INSERT INTO experiments
                      (id, project_id, name, intent, status, attempt_index,
                       mlflow_run_id, mlflow_run_name, mlflow_run_status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        experiment_id,
                        project_id,
                        f"Experiment {index}",
                        f"Intent {index}",
                        status,
                        2 if detailed else 1,
                        f"run_{index}" if detailed else "",
                        f"Run {index}" if detailed else "",
                        "FINISHED" if detailed else "",
                        created_at,
                        created_at,
                    ),
                )
                if detailed:
                    self._seed_children(
                        conn=conn,
                        project_id=project_id,
                        experiment_id=experiment_id,
                        index=index,
                        created_at=created_at,
                    )
        return experiment_ids

    def _seed_children(
        self,
        *,
        conn,
        project_id: str,
        experiment_id: str,
        index: int,
        created_at: str,
    ) -> None:
        for suffix in ("z", "a"):
            claim_id = f"claim_{project_id}_{index}_{suffix}"
            conn.execute(
                """
                INSERT INTO claims
                  (id, project_id, statement, status, confidence, created_at)
                VALUES (?, ?, ?, 'active', 'medium', ?)
                """,
                (claim_id, project_id, f"Claim {suffix}", created_at),
            )
            conn.execute(
                "INSERT INTO experiment_claims (experiment_id, claim_id) VALUES (?, ?)",
                (experiment_id, claim_id),
            )
        for attempt, role, path_suffix in (
            (2, "report", "b-report.md"),
            (1, "plan", "a-plan.md"),
        ):
            resource_id = f"res_{project_id}_{index}_{attempt}"
            conn.execute(
                """
                INSERT INTO resources
                  (id, project_id, path, kind, version_token, mtime_ns,
                   size_bytes, observed_at, created_at, updated_at)
                VALUES (?, ?, ?, 'document', ?, ?, ?, ?, ?, ?)
                """,
                (
                    resource_id,
                    project_id,
                    f"experiments/{index}/{path_suffix}",
                    f"token-{index}-{attempt}",
                    index,
                    index + attempt,
                    created_at,
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO resource_associations
                  (id, resource_id, target_type, target_id, role,
                   attempt_index, created_at, created_seq)
                VALUES (?, ?, 'experiment', ?, ?, ?, ?, ?)
                """,
                (
                    f"assoc_{resource_id}",
                    resource_id,
                    experiment_id,
                    role,
                    attempt,
                    created_at,
                    attempt,
                ),
            )
        for sequence in (1, 2):
            request_id = f"req_{project_id}_{index}_{sequence}"
            session_id = f"session_{project_id}_{index}_{sequence}"
            snapshot_id = f"experiment|{experiment_id}|complete|2|"
            conn.execute(
                """
                INSERT INTO review_requests
                  (id, project_id, target_type, target_id, role,
                   capability_hash, status, target_snapshot_id, expires_at,
                   created_at, created_seq)
                VALUES (?, ?, 'experiment', ?, 'experiment_reviewer', ?,
                        'completed', ?, ?, ?, ?)
                """,
                (
                    request_id,
                    project_id,
                    experiment_id,
                    f"hash-{project_id}-{index}-{sequence}",
                    snapshot_id,
                    "2026-07-23T00:00:00Z",
                    created_at,
                    sequence,
                ),
            )
            conn.execute(
                """
                INSERT INTO review_sessions
                  (id, request_id, independence, status, created_at)
                VALUES (?, ?, 'verified_agent_review', 'completed', ?)
                """,
                (session_id, request_id, created_at),
            )
            conn.execute(
                """
                INSERT INTO reviews
                  (id, project_id, request_id, session_id, target_snapshot_id,
                   target_type, target_id, role, verdict, findings_json,
                   evidence_json, created_at, created_seq)
                VALUES (?, ?, ?, ?, ?, 'experiment', ?,
                        'experiment_reviewer', 'pass', ?, ?, ?, ?)
                """,
                (
                    f"review_{project_id}_{index}_{sequence}",
                    project_id,
                    request_id,
                    session_id,
                    snapshot_id,
                    experiment_id,
                    json.dumps([{"sequence": sequence}]),
                    json.dumps({"resource_ids": [f"res-{sequence}"]}),
                    created_at,
                    sequence,
                ),
            )

    def _dashboard_select_count(self, *, project_id: str) -> int:
        self.store.statements.clear()
        self.app.project_dashboard_query(project_id=project_id)
        return sum(
            statement.lstrip().upper().startswith(("SELECT", "WITH"))
            for statement in self.store.statements
        )

    def test_batch_states_are_byte_and_gate_equal_to_singular_states(self) -> None:
        project_id = "proj_parity"
        experiment_ids = self._seed_project(
            project_id=project_id, active=0, terminal=2, detailed=True
        )
        with self.store.connect() as conn:
            singular = [
                self.app.experiments.get_state_with_gate(
                    experiment_id=experiment_id,
                    project_id=project_id,
                    conn=conn,
                )
                for experiment_id in experiment_ids
            ]
            batched = self.app.experiments.list_states_with_gates(
                conn=conn, project_id=project_id
            )

        self.assertEqual(len(batched), len(singular))
        for (singular_state, singular_gate), (batch_state, batch_gate) in zip(
            singular, batched, strict=True
        ):
            self.assertEqual(
                json.dumps(batch_state, separators=(",", ":")),
                json.dumps(singular_state, separators=(",", ":")),
            )
            self.assertEqual(batch_gate, singular_gate)

    def test_dashboard_query_count_is_constant_for_terminal_history(self) -> None:
        self._seed_project(project_id="proj_one", active=0, terminal=1)
        self._seed_project(project_id="proj_many", active=0, terminal=25)

        self.assertEqual(
            self._dashboard_select_count(project_id="proj_one"),
            self._dashboard_select_count(project_id="proj_many"),
        )
        # 24 = the pre-litreview 22 plus the two constant literature-signal
        # counts (papers_total, papers_unreviewed) in snapshots.read.
        self.assertEqual(self._dashboard_select_count(project_id="proj_many"), 24)

    def test_seven_active_experiments_bound_terminal_history_cost(self) -> None:
        self._seed_project(project_id="proj_active_one", active=7, terminal=1)
        self._seed_project(project_id="proj_active_many", active=7, terminal=25)

        self.assertEqual(
            self._dashboard_select_count(project_id="proj_active_one"),
            self._dashboard_select_count(project_id="proj_active_many"),
        )
        self.assertEqual(
            self._dashboard_select_count(project_id="proj_active_many"), 24
        )


class ReflectionHistoryQueryCeilingTest(unittest.TestCase):
    """Characterize representative costs of the frozen rich-history response."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = CountingStateStore(db_path=root / "state.sqlite")
        self.app = TestBrain(
            repo_root=root,
            db_path=root / "state.sqlite",
            store=self.store,
        )

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def _seed_abandoned_history(self, *, project_id: str, count: int) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                (project_id, project_id, "2026-07-22T00:00:00Z"),
            )
            for index in range(count):
                created_at = f"2026-07-22T00:{index:02d}:00Z"
                conn.execute(
                    """
                    INSERT INTO reflections
                      (id, project_id, title, status, roster_json, corpus_json,
                       created_at, updated_at, created_seq)
                    VALUES (?, ?, ?, 'abandoned', '[]', '{}', ?, ?, ?)
                    """,
                    (
                        f"syn_{project_id}_{index:03d}",
                        project_id,
                        f"Reflection {index}",
                        created_at,
                        created_at,
                        index + 1,
                    ),
                )

    def _seed_published_graph_history(self, *, project_id: str, count: int) -> None:
        graph = (
            b'{"version":1,"title":"Project logic","nodes":['
            b'{"id":"lesson","kind":"lesson","label":"Result"}],"edges":[]}'
        )
        sha256 = self.app.blobs.put(
            namespace=project_id,
            data=graph,
            content_type="application/json",
        )
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                (project_id, project_id, "2026-07-22T00:00:00Z"),
            )
            for index in range(count):
                created_at = f"2026-07-22T00:{index:02d}:00Z"
                reflection_id = f"syn_{project_id}_{index:03d}"
                resource_id = f"res_{project_id}_{index:03d}"
                version_id = f"rsv_{project_id}_{index:03d}"
                path = f"project/logic_graph_{index:03d}.json"
                conn.execute(
                    """
                    INSERT INTO reflections
                      (id, project_id, title, status, roster_json, corpus_json,
                       published_at, published_graph_version_id,
                       created_at, updated_at, created_seq)
                    VALUES (?, ?, ?, 'published', '[]', '{}', ?, ?, ?, ?, ?)
                    """,
                    (
                        reflection_id,
                        project_id,
                        f"Reflection {index}",
                        created_at,
                        version_id,
                        created_at,
                        created_at,
                        index + 1,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO resources
                      (id, project_id, path, kind, current_version_id,
                       version_token, mtime_ns, size_bytes, observed_at,
                       created_at, updated_at)
                    VALUES (?, ?, ?, 'document', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource_id,
                        project_id,
                        path,
                        version_id,
                        f"token-{index}",
                        index,
                        len(graph),
                        created_at,
                        created_at,
                        created_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO resource_versions
                      (id, resource_id, project_id, path, content_sha256,
                       size_bytes, mtime_ns, observed_at, content_type,
                       created_at, created_seq)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'application/json', ?, ?)
                    """,
                    (
                        version_id,
                        resource_id,
                        project_id,
                        path,
                        sha256,
                        len(graph),
                        index,
                        created_at,
                        created_at,
                        index + 1,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO resource_associations
                      (id, resource_id, version_id, target_type, target_id,
                       role, attempt_index, created_at, created_seq)
                    VALUES (?, ?, ?, 'reflection', ?, 'project_graph', 1, ?, ?)
                    """,
                    (
                        f"assoc_{project_id}_{index:03d}",
                        resource_id,
                        version_id,
                        reflection_id,
                        created_at,
                        index + 1,
                    ),
                )

    def _select_count(self, query, *, project_id: str) -> tuple[dict, int]:
        self.store.statements.clear()
        result = query(project_id=project_id)
        count = sum(
            statement.lstrip().upper().startswith(("SELECT", "WITH"))
            for statement in self.store.statements
        )
        return result, count

    def test_representative_reflection_histories_have_explicit_query_ceilings(
        self,
    ) -> None:
        for seed, prefix in (
            (self._seed_abandoned_history, "abandoned"),
            (self._seed_published_graph_history, "published"),
        ):
            seed(project_id=f"proj_{prefix}_one", count=1)
            seed(project_id=f"proj_{prefix}_many", count=25)

        for prefix, query, one_ceiling, many_ceiling in (
            ("abandoned", self.app.research_core.list_reflections, 8, 152),
            ("abandoned", self.app.research_core.reflection_overview, 15, 159),
            ("published", self.app.research_core.list_reflections, 8, 248),
            ("published", self.app.research_core.reflection_overview, 27, 275),
        ):
            with self.subTest(history=prefix, query=query.__name__):
                one, one_count = self._select_count(
                    query, project_id=f"proj_{prefix}_one"
                )
                many, many_count = self._select_count(
                    query, project_id=f"proj_{prefix}_many"
                )
                self.assertEqual(len(one["reflections"]), 1)
                self.assertEqual(len(many["reflections"]), 25)
                if prefix == "published":
                    self.assertTrue(
                        all(item["resources"] for item in many["reflections"])
                    )
                    self.assertTrue(
                        all(
                            item["project_graph_diff"]["available"]
                            for item in many["reflections"][1:]
                        )
                    )
                self.assertLessEqual(one_count, one_ceiling)
                self.assertLessEqual(many_count, many_ceiling)


if __name__ == "__main__":
    unittest.main()
