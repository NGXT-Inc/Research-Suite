"""Stable Research entrypoint for cross-component experiment workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from ..kernel.events import StoredEvent
from .domain.graph_lint import MAX_GRAPH_NODES, graph_problems
from .domain.paths import experiment_folder_rel
from .domain.vocabulary import (
    EXPERIMENT_ACTIVE_PROCESS_STATUSES,
    EXPERIMENT_TERMINAL_STATUSES,
)
from .experiment_views import slim_experiment_state
from .experiments import ExperimentService
from .graph_refs import GraphRefResolver
from .reflections import ReflectionService
from .transition_types import (
    CommittedExperimentTransition,
    CommittedTrackingRunRefresh,
    ExhibitVerdict,
    ExperimentState,
    PersistedRunState,
    SlimExperimentState,
)


@dataclass(frozen=True, slots=True)
class ReviewGateSnapshot:
    """Review facts needed by the pure next-action policy."""

    satisfied: bool
    blocked_reason: str
    request: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    """One transaction's Research-side facts for workflow and dashboards."""

    project_id: str
    requested_experiment_id: str | None
    project: dict[str, Any]
    claims: list[dict[str, Any]]
    experiments: list[dict[str, Any]]
    experiment_states: list[dict[str, Any]]
    selected_experiment: dict[str, Any] | None
    open_reflection: dict[str, Any] | None
    latest_published_reflection: dict[str, Any] | None
    reflection_signal: dict[str, Any]
    review_gates: dict[tuple[str, str, str], ReviewGateSnapshot]
    recent_claims: list[dict[str, Any]]
    claim_events_since_reflection: list[dict[str, Any]]


@runtime_checkable
class ResearchSnapshots(Protocol):
    def read(
        self,
        *,
        project_id: str | None = None,
        experiment_id: str | None = None,
        hydrate_all_experiments: bool = False,
        hydrate_selected_experiment: bool = True,
        dashboard_facts: bool = False,
    ) -> ResearchSnapshot: ...


@runtime_checkable
class ResearchCore(Protocol):
    def experiment_state(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> ExperimentState: ...

    def project_experiments(self, *, project_id: str | None) -> list[ExperimentState]: ...

    def transition_experiment(
        self,
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, object] | None = None,
        project_id: str | None = None,
    ) -> CommittedExperimentTransition: ...

    def record_tracking_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run: PersistedRunState,
        event_type: str | None = None,
    ) -> ExperimentState: ...

    def refresh_tracking_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run: PersistedRunState,
    ) -> CommittedTrackingRunRefresh: ...

    def record_exhibit_verdict(
        self,
        *,
        experiment_id: str,
        project_id: str,
        verdict: ExhibitVerdict,
    ) -> None: ...

    def attempt_started_running_at(self, *, experiment_id: str) -> str | None: ...

    def exhibit_path(self, *, experiment_id: str, name: str, filename: str) -> str: ...

    def present_experiment(self, state: ExperimentState) -> SlimExperimentState: ...

    def assert_experiment_in_project(
        self, *, attachment_id: str, project_id: str
    ) -> None: ...

    def reflection_state(self, *, project_id: str, reflection_id: str) -> dict[str, Any]: ...

    def reflection_overview(self, *, project_id: str) -> dict[str, Any]: ...

    def project_logic_graph_selection(self, *, project_id: str) -> dict[str, Any]: ...

    def resolve_graph_refs(
        self, *, project_id: str, graph: dict[str, Any] | None
    ) -> dict[str, Any]: ...


@runtime_checkable
class ResearchReviews(Protocol):
    def status(
        self, *, target_type: str, target_id: str, project_id: str | None = None
    ) -> dict[str, Any]: ...

    def latest_submitted_event(
        self, *, target_type: str, target_id: str, project_id: str | None = None
    ) -> StoredEvent | None: ...


class ResearchCoreFacade:
    """Narrow adapter over the already-composed experiment service."""

    __slots__ = ("_experiments", "_graph_refs", "_reflections")

    def __init__(
        self,
        experiments: ExperimentService,
        *,
        reflections: ReflectionService | None = None,
        graph_refs: GraphRefResolver | None = None,
    ) -> None:
        self._experiments = experiments
        self._reflections = reflections
        self._graph_refs = graph_refs

    def experiment_state(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> ExperimentState:
        return cast(
            ExperimentState,
            self._experiments.get_state(
                experiment_id=experiment_id, project_id=project_id
            ),
        )

    def project_experiments(self, *, project_id: str | None) -> list[ExperimentState]:
        return cast(
            list[ExperimentState],
            self._experiments.list_experiments(project_id=project_id)["experiments"],
        )

    def transition_experiment(
        self,
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, object] | None = None,
        project_id: str | None = None,
    ) -> CommittedExperimentTransition:
        return self._experiments.transition_with_event(
            experiment_id=experiment_id,
            transition=transition,
            evidence=evidence,
            project_id=project_id,
        )

    def record_tracking_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run: PersistedRunState,
        event_type: str | None = None,
    ) -> ExperimentState:
        return cast(
            ExperimentState,
            self._experiments.record_mlflow_run(
                project_id=project_id,
                experiment_id=experiment_id,
                run=run,
                event_type=event_type,
            ),
        )

    def refresh_tracking_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run: PersistedRunState,
    ) -> CommittedTrackingRunRefresh:
        return cast(
            CommittedTrackingRunRefresh,
            self._experiments.record_mlflow_run(
                project_id=project_id,
                experiment_id=experiment_id,
                run=run,
                event_type="experiment.mlflow_run_refreshed",
                return_event=True,
            ),
        )

    def record_exhibit_verdict(
        self,
        *,
        experiment_id: str,
        project_id: str,
        verdict: ExhibitVerdict,
    ) -> None:
        self._experiments.record_exhibit_verdict(
            experiment_id=experiment_id,
            project_id=project_id,
            verdict=verdict,
        )

    def attempt_started_running_at(self, *, experiment_id: str) -> str | None:
        return self._experiments.attempt_started_running_at(
            experiment_id=experiment_id
        )

    def exhibit_path(self, *, experiment_id: str, name: str, filename: str) -> str:
        return f"{experiment_folder_rel(experiment_id=experiment_id, name=name)}{filename}"

    def present_experiment(self, state: ExperimentState) -> SlimExperimentState:
        return cast(SlimExperimentState, slim_experiment_state(state))

    def assert_experiment_in_project(
        self, *, attachment_id: str, project_id: str
    ) -> None:
        self._experiments.assert_in_project(
            experiment_id=attachment_id, project_id=project_id
        )

    def reflection_state(
        self, *, project_id: str, reflection_id: str
    ) -> dict[str, Any]:
        return self._reflection_service().get_state(
            project_id=project_id, reflection_id=reflection_id
        )

    def reflection_overview(self, *, project_id: str) -> dict[str, Any]:
        return self._reflection_service().overview(project_id=project_id)

    def project_logic_graph_selection(self, *, project_id: str) -> dict[str, Any]:
        return self._reflection_service().project_logic_graph_selection(
            project_id=project_id
        )

    def resolve_graph_refs(
        self, *, project_id: str, graph: dict[str, Any] | None
    ) -> dict[str, Any]:
        if self._graph_refs is None:
            raise RuntimeError("graph reference resolver is not configured")
        return self._graph_refs.resolve_index(project_id=project_id, graph=graph)

    def _reflection_service(self) -> ReflectionService:
        if self._reflections is None:
            raise RuntimeError("reflection service is not configured")
        return self._reflections


__all__ = [
    "CommittedExperimentTransition",
    "CommittedTrackingRunRefresh",
    "EXPERIMENT_ACTIVE_PROCESS_STATUSES",
    "EXPERIMENT_TERMINAL_STATUSES",
    "ExhibitVerdict",
    "ExperimentState",
    "MAX_GRAPH_NODES",
    "PersistedRunState",
    "ResearchCore",
    "ResearchCoreFacade",
    "ResearchReviews",
    "ResearchSnapshot",
    "ResearchSnapshots",
    "ReviewGateSnapshot",
    "SlimExperimentState",
    "graph_problems",
]
