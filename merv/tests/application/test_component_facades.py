from __future__ import annotations

import unittest
from typing import get_type_hints

from merv.brain.application.experiments.transition import (
    TransitionExperiment,
    TransitionResponse,
)
from merv.brain.application.reflections import ReflectionCommands
from merv.brain.artifacts.facade import Artifacts, ArtifactsFacade
from merv.brain.feed.facade import Feed, FeedFacade
from merv.brain.kernel.events import StoredEvent, freeze_json_object
from merv.brain.research_core.facade import (
    CommittedExperimentTransition,
    CommittedTrackingRunRefresh,
    ResearchCore,
    ResearchCoreFacade,
    ResearchReviews,
)
from merv.brain.research_core.transition_types import (
    CommittedExperimentTransition as OwnedCommittedTransition,
    ExperimentState,
)


def _committed(state, *, event_type="experiment.transitioned"):
    return CommittedExperimentTransition(
        state=state,
        event=StoredEvent(
            id=7,
            project_id="proj_1",
            type=event_type,
            target_type="experiment",
            target_id="exp_1",
            payload=freeze_json_object({"transition": "start_running"}),
            created_at="2026-07-19T12:00:00Z",
        ),
    )


class RecordingExperimentService:
    def __init__(self) -> None:
        self.calls = []
        self.state = {
            "id": "exp_1",
            "project_id": "proj_1",
            "name": "First Run",
            "status": "running",
            "attempt_index": 2,
            "mlflow_run": None,
        }
        self.committed = _committed(self.state)

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self.state

    def get_state(self, **kwargs):
        self.calls.append(("get_state", kwargs))
        return self.state

    def list_experiments(self, **kwargs):
        self.calls.append(("list_experiments", kwargs))
        return {"experiments": [self.state]}

    def transition_with_event(self, **kwargs):
        self.calls.append(("transition_with_event", kwargs))
        return self.committed

    def record_mlflow_run(self, **kwargs):
        self.calls.append(("record_mlflow_run", kwargs))
        if kwargs.get("return_event"):
            return _committed(
                self.state, event_type="experiment.mlflow_run_refreshed"
            )
        return self.state

    def record_exhibit_verdict(self, **kwargs):
        self.calls.append(("record_exhibit_verdict", kwargs))

    def attempt_started_running_at(self, **kwargs):
        self.calls.append(("attempt_started_running_at", kwargs))
        return "2026-07-19T11:00:00Z"


class RecordingResourcesService:
    def __init__(self) -> None:
        self.calls = []
        self.sources = [
            {
                "path": "experiments/first/results.json",
                "version_id": "rver_1",
                "sha256": "abc",
                "observed_at": "2026-07-19T11:30:00Z",
                "data": {"accuracy": 0.8},
            }
        ]

    def metric_file_sources(self, **kwargs):
        self.calls.append(("metric_file_sources", kwargs))
        return self.sources

    def pin_system_artifact(self, **kwargs):
        self.calls.append(("pin_system_artifact", kwargs))
        return {"association": "internal result intentionally hidden"}

    def resolve_resource_reference(self, **kwargs):
        self.calls.append(("resolve_resource_reference", kwargs))
        return {"type": "resource", "resolved": True, "resource_id": "res_1"}


class RecordingFeedService:
    def __init__(self) -> None:
        self.calls = []

    def feed_note_for(self, **kwargs):
        self.calls.append(("feed_note_for", kwargs))
        return "Share the result."


class RecordingReflectionService:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"id": "syn_1", "lenses": kwargs["lenses"]}

    def get_state(self, **kwargs):
        self.calls.append(("get_state", kwargs))
        return {"id": kwargs["reflection_id"]}

    def list_reflections(self, **kwargs):
        self.calls.append(("list_reflections", kwargs))
        return {"count": 1, "reflections": [{"id": "syn_1"}]}

    def transition(self, **kwargs):
        self.calls.append(("transition", kwargs))
        return {"id": kwargs["reflection_id"], "status": kwargs["transition"]}


class RecordingResearchCoreFake:
    """Application tests can replace Research without importing its internals."""

    def create_experiment(self, **kwargs):
        return {"id": "exp_fake", **kwargs}

    def experiment_state(self, **kwargs):
        return {"id": kwargs["experiment_id"]}

    def project_experiments(self, **kwargs):
        return []

    def transition_experiment(self, **kwargs):
        return _committed({"id": kwargs["experiment_id"]})

    def record_tracking_run(self, **kwargs):
        return {"id": kwargs["experiment_id"], "mlflow_run": kwargs["run"]}

    def refresh_tracking_run(self, **kwargs):
        return _committed(
            {"id": kwargs["experiment_id"], "mlflow_run": kwargs["run"]},
            event_type="experiment.mlflow_run_refreshed",
        )

    def record_exhibit_verdict(self, **kwargs):
        return None

    def attempt_started_running_at(self, **kwargs):
        return None

    def exhibit_path(self, **kwargs):
        return f"experiments/fake/{kwargs['filename']}"

    def assert_experiment_in_project(self, **_kwargs):
        return None

    def reflection_state(self, **_kwargs):
        return {}

    def create_reflection(self, **_kwargs):
        return {}

    def list_reflections(self, **_kwargs):
        return {"count": 0, "reflections": []}

    def transition_reflection(self, **_kwargs):
        return {}

    def reflection_overview(self, **_kwargs):
        return {}

    def project_logic_graph_selection(self, **_kwargs):
        return {}

    def resolve_research_graph_refs(self, **_kwargs):
        return {}


class RecordingArtifactsFake:
    def metric_file_sources(self, **kwargs):
        return []

    def pin_system_artifact(self, **kwargs):
        return None

    def resolve_resource(self, **_kwargs):
        return {}

    def select_resource_text(self, **_kwargs):
        return None

    def submitted_figure(self, **_kwargs):
        return None

    def submitted_text_for_version(self, **_kwargs):
        return None

    def resolve_resource_reference(self, **_kwargs):
        return None


class RecordingResearchReviewsFake:
    def status(self, **_kwargs):
        return {"requests": [], "reviews": []}

    def latest_submitted_event(self, **_kwargs):
        return None


class RecordingFeedFake:
    def transition_advisory(self, **kwargs):
        return None


class ComponentFacadeTest(unittest.TestCase):
    def test_structural_contracts_accept_isolated_application_fakes(self) -> None:
        research = RecordingResearchCoreFake()
        artifacts = RecordingArtifactsFake()
        feed = RecordingFeedFake()
        reviews = RecordingResearchReviewsFake()

        self.assertIsInstance(research, ResearchCore)
        self.assertIsInstance(artifacts, Artifacts)
        self.assertIsInstance(feed, Feed)
        self.assertIsInstance(reviews, ResearchReviews)
        self.assertEqual(
            research.experiment_state(experiment_id="exp_fake"),
            {"id": "exp_fake"},
        )
        self.assertEqual(
            research.exhibit_path(
                experiment_id="exp_fake", name="Fake", filename="exhibit.json"
            ),
            "experiments/fake/exhibit.json",
        )
        self.assertEqual(
            artifacts.metric_file_sources(
                experiment_id="exp_fake", attempt_index=1
            ),
            [],
        )
        self.assertIsNone(
            feed.transition_advisory(
                project_id="proj_fake",
                experiment_id="exp_fake",
                event="experiment_complete",
            )
        )

    def test_research_facade_delegates_to_the_exact_experiment_service(self) -> None:
        service = RecordingExperimentService()
        facade = ResearchCoreFacade(service)

        self.assertIs(facade._experiments, service)
        self.assertIsInstance(facade, ResearchCore)
        self.assertIs(
            facade.create_experiment(
                name="First Run", intent="Test the boundary", project_id="proj_1"
            ),
            service.state,
        )
        self.assertEqual(
            service.calls.pop(0),
            (
                "create",
                {
                    "name": "First Run",
                    "intent": "Test the boundary",
                    "project_id": "proj_1",
                },
            ),
        )
        self.assertIs(
            facade.experiment_state(experiment_id="exp_1", project_id="proj_1"),
            service.state,
        )
        self.assertEqual(facade.project_experiments(project_id="proj_1"), [service.state])
        self.assertIs(
            facade.transition_experiment(
                experiment_id="exp_1",
                transition="start_running",
                evidence={"note": "go"},
                project_id="proj_1",
            ),
            service.committed,
        )
        run = {"run_id": "run_1", "status": "RUNNING"}
        self.assertIs(
            facade.record_tracking_run(
                project_id="proj_1",
                experiment_id="exp_1",
                run=run,
                event_type="experiment.mlflow_run_refreshed",
            ),
            service.state,
        )
        refreshed = facade.refresh_tracking_run(
            project_id="proj_1", experiment_id="exp_1", run=run
        )
        self.assertIsInstance(refreshed, CommittedTrackingRunRefresh)
        self.assertEqual(refreshed.event.type, "experiment.mlflow_run_refreshed")
        verdict = {"runs_found": 1, "pinned": True}
        self.assertIsNone(
            facade.record_exhibit_verdict(
                experiment_id="exp_1", project_id="proj_1", verdict=verdict
            )
        )
        self.assertEqual(
            facade.attempt_started_running_at(experiment_id="exp_1"),
            "2026-07-19T11:00:00Z",
        )
        self.assertEqual(
            facade.exhibit_path(
                experiment_id="exp_1",
                name="First Run",
                filename="metrics_exhibit.json",
            ),
            "experiments/First_Run/metrics_exhibit.json",
        )
        self.assertEqual(
            service.calls,
            [
                ("get_state", {"experiment_id": "exp_1", "project_id": "proj_1"}),
                ("list_experiments", {"project_id": "proj_1"}),
                (
                    "transition_with_event",
                    {
                        "experiment_id": "exp_1",
                        "transition": "start_running",
                        "evidence": {"note": "go"},
                        "project_id": "proj_1",
                    },
                ),
                (
                    "record_mlflow_run",
                    {
                        "project_id": "proj_1",
                        "experiment_id": "exp_1",
                        "run": run,
                        "event_type": "experiment.mlflow_run_refreshed",
                    },
                ),
                (
                    "record_mlflow_run",
                    {
                        "project_id": "proj_1",
                        "experiment_id": "exp_1",
                        "run": run,
                        "event_type": "experiment.mlflow_run_refreshed",
                        "return_event": True,
                    },
                ),
                (
                    "record_exhibit_verdict",
                    {
                        "experiment_id": "exp_1",
                        "project_id": "proj_1",
                        "verdict": verdict,
                    },
                ),
                ("attempt_started_running_at", {"experiment_id": "exp_1"}),
            ],
        )

    def test_research_facade_exposes_typed_reflection_commands(self) -> None:
        reflections = RecordingReflectionService()
        facade = ResearchCoreFacade(
            RecordingExperimentService(), reflections=reflections
        )

        self.assertIsInstance(facade, ResearchCore)
        self.assertEqual(
            facade.create_reflection(project_id="proj_1", title="Wave"),
            {"id": "syn_1", "lenses": []},
        )
        self.assertEqual(
            facade.reflection_state(project_id="proj_1", reflection_id="syn_1"),
            {"id": "syn_1"},
        )
        self.assertEqual(
            facade.list_reflections(project_id="proj_1"),
            {"count": 1, "reflections": [{"id": "syn_1"}]},
        )
        self.assertEqual(
            facade.transition_reflection(
                project_id="proj_1", reflection_id="syn_1", transition="publish"
            ),
            {"id": "syn_1", "status": "publish"},
        )
        self.assertEqual(
            reflections.calls,
            [
                ("create", {"project_id": "proj_1", "title": "Wave", "lenses": []}),
                ("get_state", {"project_id": "proj_1", "reflection_id": "syn_1"}),
                ("list_reflections", {"project_id": "proj_1"}),
                (
                    "transition",
                    {
                        "project_id": "proj_1",
                        "reflection_id": "syn_1",
                        "transition": "publish",
                    },
                ),
            ],
        )

    def test_reflection_list_restores_the_delivery_count_and_key_order(self) -> None:
        research = RecordingResearchCoreFake()
        research.list_reflections = lambda **_kwargs: {"reflections": [{"id": "syn_1"}]}

        result = ReflectionCommands(reflections=research).list(project_id="proj_1")

        self.assertEqual(list(result), ["count", "reflections"])
        self.assertEqual(result, {"count": 1, "reflections": [{"id": "syn_1"}]})

    def test_artifacts_facade_normalizes_only_the_experiment_target(self) -> None:
        service = RecordingResourcesService()
        facade = ArtifactsFacade(service)

        self.assertIs(facade._resources, service)
        self.assertIsInstance(facade, Artifacts)
        self.assertIs(
            facade.metric_file_sources(experiment_id="exp_1", attempt_index=2),
            service.sources,
        )
        self.assertIsNone(
            facade.pin_system_artifact(
                path="experiments/First_Run/metrics_exhibit.json",
                experiment_id="exp_1",
                role="exhibit",
                content_bytes=b"{}",
                content_type="application/json",
                title="Metrics exhibit",
                kind="result",
                project_id="proj_1",
            )
        )
        self.assertEqual(
            facade.resolve_resource_reference(
                project_id="proj_1", ref="results.json"
            ),
            {"type": "resource", "resolved": True, "resource_id": "res_1"},
        )
        self.assertEqual(
            service.calls,
            [
                (
                    "metric_file_sources",
                    {"target_id": "exp_1", "attempt_index": 2},
                ),
                (
                    "pin_system_artifact",
                    {
                        "path": "experiments/First_Run/metrics_exhibit.json",
                        "target_type": "experiment",
                        "target_id": "exp_1",
                        "role": "exhibit",
                        "content_bytes": b"{}",
                        "content_type": "application/json",
                        "title": "Metrics exhibit",
                        "kind": "result",
                        "project_id": "proj_1",
                    },
                ),
                (
                    "resolve_resource_reference",
                    {"project_id": "proj_1", "ref": "results.json"},
                ),
            ],
        )

    def test_feed_facade_normalizes_only_the_experiment_identity(self) -> None:
        service = RecordingFeedService()
        facade = FeedFacade(service)

        self.assertIs(facade._feed, service)
        self.assertIsInstance(facade, Feed)
        self.assertEqual(
            facade.transition_advisory(
                project_id="proj_1",
                experiment_id="exp_1",
                event="experiment_complete",
            ),
            "Share the result.",
        )
        self.assertEqual(
            service.calls,
            [
                (
                    "feed_note_for",
                    {
                        "project_id": "proj_1",
                        "entity_id": "exp_1",
                        "event": "experiment_complete",
                    },
                )
            ],
        )

    def test_committed_transition_is_the_research_owned_identity(self) -> None:
        self.assertIs(CommittedExperimentTransition, OwnedCommittedTransition)
        self.assertIs(
            get_type_hints(CommittedExperimentTransition)["state"], ExperimentState
        )
        self.assertIs(
            get_type_hints(TransitionExperiment.execute)["return"], TransitionResponse
        )


if __name__ == "__main__":
    unittest.main()
