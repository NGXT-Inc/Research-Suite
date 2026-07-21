from __future__ import annotations

import unittest

from merv.brain.application.queries import (
    ComputeCostQuery,
    ExperimentFigureQuery,
    LogicGraphQuery,
    MlflowOverviewQuery,
    TenantCountersQuery,
)


class RecordingQuery:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class RecordingTracking:
    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.calls = []

    def health(self):
        self.calls.append(("health", {}))
        return {"configured": True, "reachable": self.reachable}

    def results_metrics(self, **kwargs):
        self.calls.append(("results_metrics", kwargs))
        return {
            "experiment_id": kwargs["experiment_id"],
            "available": True,
            "dashboard_experiment_url": "https://tracking.test/#/experiments/7",
        }

    def namespace_experiments(self, **kwargs):
        self.calls.append(("namespace_experiments", kwargs))
        project_id = kwargs["project_id"]
        return [
            {"name": f"merv/{project_id}/exp_1", "experiment_id": "7"},
            {"name": f"merv/{project_id}/stray", "experiment_id": "8"},
        ]


class GraphResearch:
    def __init__(self) -> None:
        self.resolved = []

    def experiment_state(self, **_kwargs):
        return {
            "id": "exp_1",
            "status": "running",
            "attempt_index": 2,
            "resources": [
                {
                    "id": "res_old",
                    "path": "old.json",
                    "association_role": "graph",
                    "association_attempt_index": 1,
                    "association_rowid": 1,
                    "association_version_id": "ver_old",
                },
                {
                    "id": "res_new",
                    "path": "new.json",
                    "association_role": "graph",
                    "association_attempt_index": 2,
                    "association_rowid": 2,
                    "association_version_id": "ver_new",
                },
            ],
        }

    def reflection_state(self, **_kwargs):
        return {"id": "syn_1", "attempt_index": 1, "resources": []}

    def reflection_overview(self, **_kwargs):
        return {"reflections": [{"id": "syn_1"}]}

    def project_logic_graph_selection(self, **_kwargs):
        return {"reflection": None, "graph_resource": None, "signal": "stale"}

    def resolve_research_graph_refs(self, **kwargs):
        self.resolved.append(kwargs)
        return {
            ref: {"type": "claim", "resolved": True}
            for ref in kwargs["refs"]
            if ref == "claim_1"
        }


class GraphArtifacts:
    def __init__(self) -> None:
        self.resolved = []

    def submitted_text_for_version(self, *, version_id):
        if version_id == "ver_new":
            return '{"version":1,"nodes":[{"id":"n","label":"New","refs":["claim_1"]}]}'
        return None

    def resolve_resource_reference(self, *, project_id, ref):
        self.resolved.append({"project_id": project_id, "ref": ref})
        return None


class ApplicationQueryTest(unittest.TestCase):
    def test_tenant_counters_join_kernel_and_sandbox_readers(self) -> None:
        events = RecordingQuery(4)
        generations = RecordingQuery(
            {"sandbox_generations": 2, "sandbox_hours": 3.5}
        )

        result = TenantCountersQuery(
            event_count=events,
            generation_counters=generations,
        )(tenant_id="tenant_1")

        self.assertEqual(
            result,
            {
                "tenant_id": "tenant_1",
                "tool_calls": 4,
                "sandbox_generations": 2,
                "sandbox_hours": 3.5,
            },
        )
        self.assertEqual(events.calls, [{"tenant_id": "tenant_1"}])
        self.assertEqual(generations.calls, [{"tenant_id": "tenant_1"}])

    def test_compute_cost_hydrates_experiment_names(self) -> None:
        spend = RecordingQuery(
            {
                "total_usd": 3.5,
                "by_experiment": [
                    {"experiment_id": "exp_1"},
                    {"experiment_id": "exp_missing"},
                ],
            }
        )
        experiments = RecordingQuery(
            [{"id": "exp_1", "name": "First"}, {"id": "exp_2", "name": "Second"}]
        )

        result = ComputeCostQuery(
            project_spend=spend, experiments=experiments
        )(project_id="proj_1")

        self.assertEqual(
            result["by_experiment"],
            [
                {"experiment_id": "exp_1", "experiment_name": "First"},
                {"experiment_id": "exp_missing", "experiment_name": ""},
            ],
        )

    def test_logic_graph_query_owns_selection_parsing_lint_and_ref_resolution(self) -> None:
        research = GraphResearch()
        artifacts = GraphArtifacts()
        query = LogicGraphQuery(research=research, artifacts=artifacts)

        result = query.experiment(project_id="proj_1", experiment_id="exp_1")

        self.assertTrue(result["available"])
        self.assertEqual(result["resource_id"], "res_new")
        self.assertEqual(result["graph"]["nodes"][0]["label"], "New")
        self.assertEqual(result["problems"], [])
        self.assertEqual(
            result["ref_index"],
            {"claim_1": {"type": "claim", "resolved": True}},
        )
        self.assertEqual(
            research.resolved,
            [{"project_id": "proj_1", "refs": ("claim_1",)}],
        )
        self.assertEqual(artifacts.resolved, [])

    def test_logic_graph_query_composes_refs_in_first_seen_order(self) -> None:
        class MixedResearch(GraphResearch):
            def experiment_state(self, **_kwargs):
                state = super().experiment_state()
                state["resources"][-1]["association_version_id"] = "ver_mixed"
                return state

            def resolve_research_graph_refs(self, **kwargs):
                self.resolved.append(kwargs)
                return {
                    "claim_1": {"type": "claim", "resolved": True},
                    "exp_missing": {"type": "unknown", "resolved": False},
                }

        class MixedArtifacts(GraphArtifacts):
            def submitted_text_for_version(self, *, version_id):
                if version_id == "ver_mixed":
                    return (
                        '{"version":1,"nodes":['
                        '{"id":"a","label":"A","refs":'
                        '["claim_1","results.json","claim_1","exp_missing"]},'
                        '{"id":"b","label":"B","refs":'
                        '["res_missing","ghost.json"," ",7]}]}'
                    )
                return None

            def resolve_resource_reference(self, *, project_id, ref):
                self.resolved.append({"project_id": project_id, "ref": ref})
                if ref == "results.json":
                    return {
                        "type": "resource",
                        "resolved": True,
                        "resource_id": "res_results",
                    }
                return None

        research = MixedResearch()
        artifacts = MixedArtifacts()

        result = LogicGraphQuery(research=research, artifacts=artifacts).experiment(
            project_id="proj_1", experiment_id="exp_1"
        )

        self.assertEqual(
            list(result["ref_index"]),
            [
                "claim_1",
                "results.json",
                "exp_missing",
                "res_missing",
                "ghost.json",
            ],
        )
        self.assertEqual(
            result["ref_index"]["exp_missing"],
            {"type": "unknown", "resolved": False},
        )
        self.assertEqual(
            result["ref_index"]["res_missing"],
            {"type": "unknown", "resolved": False},
        )
        self.assertIn(
            "not a registered resource",
            result["ref_index"]["ghost.json"]["hint"],
        )
        self.assertEqual(
            research.resolved,
            [
                {
                    "project_id": "proj_1",
                    "refs": (
                        "claim_1",
                        "results.json",
                        "exp_missing",
                        "res_missing",
                        "ghost.json",
                    ),
                }
            ],
        )
        self.assertEqual(
            [call["ref"] for call in artifacts.resolved],
            ["results.json", "res_missing", "ghost.json"],
        )

    def test_project_graph_keeps_signal_when_no_reflection_exists(self) -> None:
        result = LogicGraphQuery(
            research=GraphResearch(), artifacts=GraphArtifacts()
        ).project(project_id="proj_1")

        self.assertEqual(
            result,
            {
                "max_nodes": 16,
                "signal": "stale",
                "available": False,
                "reflection": None,
                "graph": None,
                "problems": [],
            },
        )

    def test_project_graph_presents_semantic_reflection_signal(self) -> None:
        class SemanticSignalResearch(GraphResearch):
            def project_logic_graph_selection(self, **_kwargs):
                return {
                    "reflection": None,
                    "graph_resource": None,
                    "signal": {
                        "terminal_experiments": 3,
                        "covered_terminal_experiments": 0,
                        "new_terminal_since_publish": 3,
                        "claims_changed_since_publish": 0,
                        "contradicted_flip": False,
                        "last_published_reflection_id": None,
                        "stale": True,
                        "experiment_create_blocked": False,
                    },
                }

        result = LogicGraphQuery(
            research=SemanticSignalResearch(), artifacts=GraphArtifacts()
        ).project(project_id="proj_1")

        self.assertEqual(list(result["signal"])[-1], "hint")
        self.assertIn("first reflection", result["signal"]["hint"])

    def test_mlflow_overview_preserves_mapping_and_history_policy(self) -> None:
        tracking = RecordingTracking()
        query = MlflowOverviewQuery(
            experiments=RecordingQuery(
                {
                    "experiments": [
                        {
                            "id": "exp_1",
                            "name": "Experiment One",
                            "status": "running",
                            "intent": "Measure it",
                        }
                    ]
                }
            ),
            tracking=tracking,
        )

        result = query(project_id="proj_1")

        self.assertEqual(result["experiments"][0]["mlflow_experiment_name"], "merv/proj_1/exp_1")
        self.assertEqual(
            result["experiments"][0]["dashboard_experiment_url"],
            "https://tracking.test/#/experiments/7",
        )
        self.assertEqual(
            result["unmapped_mlflow_experiments"],
            [{"name": "merv/proj_1/stray", "experiment_id": "8"}],
        )
        self.assertIn(
            (
                "results_metrics",
                {
                    "project_id": "proj_1",
                    "experiment_id": "exp_1",
                    "include_history": False,
                },
            ),
            tracking.calls,
        )

    def test_mlflow_overview_short_circuits_an_unreachable_adapter(self) -> None:
        tracking = RecordingTracking(reachable=False)
        query = MlflowOverviewQuery(
            experiments=RecordingQuery({"experiments": [{"id": "exp_1"}]}),
            tracking=tracking,
        )

        result = query(project_id="proj_1")

        self.assertEqual(
            result["experiments"][0]["metrics"],
            {
                "experiment_id": "exp_1",
                "available": False,
                "source": "mlflow",
                "hint": "MLflow unreachable.",
            },
        )
        self.assertEqual(result["unmapped_mlflow_experiments"], [])
        self.assertEqual(tracking.calls, [("health", {})])

    def test_figure_gathers_review_and_sandbox_facts_before_projection(self) -> None:
        experiment = {
            "id": "exp_1",
            "intent": "Test",
            "status": "running",
            "attempt_index": 2,
            "resources": [],
            "reviews": [{"id": "review_1", "target_snapshot_id": "snap_1", "verdict": "pass"}],
            "tested_claims": [],
        }
        state = RecordingQuery(experiment)
        snapshot = RecordingQuery({"attempt_index": 1})
        open_reviews = RecordingQuery([])
        sandbox_row = RecordingQuery({"status": "running", "gpu": "H100"})
        sandbox_view = RecordingQuery({"status": "running", "gpu": "H100"})
        query = ExperimentFigureQuery(
            experiment_state=state,
            review_snapshot=snapshot,
            open_reviews=open_reviews,
            sandbox_row=sandbox_row,
            sandbox_view=sandbox_view,
            sandbox_status_active={"running"}.__contains__,
        )

        result = query(project_id="proj_1", experiment_id="exp_1")

        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(nodes["review:review_1"]["group"], "attempt:1")
        self.assertEqual(nodes["sandbox"]["status"], "active")
        self.assertEqual(
            state.calls, [{"experiment_id": "exp_1", "project_id": "proj_1"}]
        )
        self.assertEqual(snapshot.calls, [{"snapshot_id": "snap_1"}])
        self.assertEqual(
            open_reviews.calls,
            [{"project_id": "proj_1", "experiment_id": "exp_1"}],
        )


if __name__ == "__main__":
    unittest.main()
