"""Application commands and queries for experiment tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict, cast

from ...feed.facade import Feed
from ...research_core.facade import (
    ExperimentState,
    PersistedRunState,
    ResearchCore,
)
from ..events import EventDispatcher
from ..ports.tracking import (
    ExperimentTracking,
    FinalizeRunResult,
    TrackingContextPayload,
)
from ..ports.storage import ProducedObjectCatalog
from .tracking_policy import mlflow_experiment_name, mlflow_visible_for_status
from .presentation import (
    SlimExperimentState,
    rich_experiment_state,
    slim_experiment_state,
)
from .tracking_presentation import (
    tracking_connection,
    tracking_context_response,
    with_tracking_if_visible,
)


class TrackingContextResponse(TypedDict, total=False):
    project_id: str
    experiment_id: str
    scope: str
    mlflow: TrackingContextPayload
    guidance: str


class FinalizeTrackingResponse(FinalizeRunResult, total=False):
    project_id: str
    experiment_id: str
    experiment: SlimExperimentState
    configured: bool
    run_id: str
    error: str
    feed_note: str


class ExperimentDetailResponse(ExperimentState, total=False):
    mlflow: dict[str, Any]


class GetTrackingContext:
    """Build project or experiment tracking connection context."""

    def __init__(
        self, *, research: ResearchCore, tracking: ExperimentTracking | None
    ) -> None:
        self.research, self.tracking = research, tracking

    def execute(
        self, *, project_id: str, experiment_id: str | None = None
    ) -> TrackingContextResponse:
        if experiment_id:
            state = self.research.experiment_state(
                experiment_id=experiment_id, project_id=project_id
            )
            resolved_project_id = str(state.get("project_id") or project_id or "")
            block = tracking_connection(
                tracking=self.tracking,
                project_id=resolved_project_id,
                experiment_id=experiment_id,
                include_credentials=True,
                run=state.get("mlflow_run"),
            )
            return cast(
                TrackingContextResponse,
                tracking_context_response(
                    project_id=resolved_project_id,
                    experiment_id=experiment_id,
                    tracking=block,
                ),
            )
        block: dict[str, Any] = (
            {"configured": False}
            if self.tracking is None or not project_id
            else dict(
                self.tracking.project_context(
                    project_id=project_id, include_credentials=True
                )
            )
        )
        if self.tracking is not None and project_id:
            block["experiments"] = [
                {
                    "experiment_id": str(state.get("id") or ""),
                    "name": str(state.get("name") or state.get("id") or ""),
                    "status": str(state.get("status") or ""),
                    "intent": str(state.get("intent") or ""),
                    "mlflow_experiment_name": mlflow_experiment_name(
                        project_id=project_id,
                        experiment_id=str(state.get("id") or ""),
                    ),
                }
                for state in self.research.project_experiments(project_id=project_id)
                if state.get("id")
            ]
        return cast(
            TrackingContextResponse,
            tracking_context_response(
                project_id=project_id, experiment_id=None, tracking=block
            ),
        )

@dataclass(slots=True)
class AgentExperimentQuery:
    """Agent-facing experiment state, including credential-bearing tracking."""

    research: ResearchCore
    objects: ProducedObjectCatalog
    tracking: ExperimentTracking | None

    def __call__(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> SlimExperimentState:
        state = self.research.experiment_state(
            experiment_id=experiment_id, project_id=project_id
        )
        resolved_project_id = str(state.get("project_id") or project_id or "")
        storage_objects = self.objects.by_experiment(
            project_id=resolved_project_id, experiment_ids=(experiment_id,)
        )[experiment_id]
        response = dict(
            slim_experiment_state(state, storage_objects=storage_objects)
        )
        return cast(
            SlimExperimentState,
            with_tracking_if_visible(
                state=response,
                tracking=self.tracking,
                project_id=resolved_project_id,
                experiment_id=experiment_id,
                include_credentials=True,
            ),
        )


@dataclass(slots=True)
class ExperimentDetailQuery:
    """UI experiment detail with redacted tracking connection data."""

    research: ResearchCore
    objects: ProducedObjectCatalog
    tracking: ExperimentTracking | None

    def __call__(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> ExperimentDetailResponse:
        state = self.research.experiment_state(
            experiment_id=experiment_id, project_id=project_id
        )
        resolved_project_id = str(state.get("project_id") or project_id or "")
        response = cast(
            ExperimentDetailResponse,
            rich_experiment_state(
                state,
                storage_objects=self.objects.by_experiment(
                    project_id=resolved_project_id,
                    experiment_ids=(experiment_id,),
                )[experiment_id],
            ),
        )
        if mlflow_visible_for_status(state.get("status")):
            response["mlflow"] = tracking_connection(
                tracking=self.tracking,
                project_id=resolved_project_id,
                experiment_id=experiment_id,
                include_credentials=False,
                run=state.get("mlflow_run"),
            )
        return response


class FinalizeTrackingRun:
    """Finalize a run, persist canonical readback, then run late reactions."""

    def __init__(
        self,
        *,
        research: ResearchCore,
        feed: Feed,
        tracking: ExperimentTracking | None,
        dispatcher: EventDispatcher,
        objects: ProducedObjectCatalog,
    ) -> None:
        self.research, self.feed, self.tracking = research, feed, tracking
        self.dispatcher = dispatcher
        self.objects = objects

    def execute(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run_id: str | None = None,
        status: str | None = "FINISHED",
        wait_seconds: float = 2.0,
    ) -> FinalizeTrackingResponse:
        state = self.research.experiment_state(
            experiment_id=experiment_id, project_id=project_id
        )
        resolved_project_id = str(state.get("project_id") or project_id or "")
        existing_run = state.get("mlflow_run") or {}
        resolved_run_id = str(run_id or existing_run.get("run_id") or "")
        if self.tracking is None:
            return {
                "project_id": resolved_project_id,
                "experiment_id": experiment_id,
                "configured": False,
                "run_id": resolved_run_id,
                "error": "MLflow tracking is not configured on this backend.",
            }
        storage_objects = self.objects.by_experiment(
            project_id=resolved_project_id, experiment_ids=(experiment_id,)
        )[experiment_id]
        result = self.tracking.finalize_run(
            project_id=resolved_project_id,
            experiment_id=experiment_id,
            run_id=resolved_run_id,
            status=status,
            wait_seconds=wait_seconds,
        )
        run = result.get("run")
        committed = None
        persisted_run_id = str(existing_run.get("run_id") or "")
        if (
            isinstance(run, dict)
            and run.get("run_id")
            and (not persisted_run_id or str(run.get("run_id")) == persisted_run_id)
        ):
            committed = self.research.refresh_tracking_run(
                project_id=resolved_project_id,
                experiment_id=experiment_id,
                run=cast(PersistedRunState, run),
            )
            state = committed.state
        experiment = dict(
            slim_experiment_state(state, storage_objects=storage_objects)
        )
        with_tracking_if_visible(
            state=experiment,
            tracking=self.tracking,
            project_id=resolved_project_id,
            experiment_id=experiment_id,
            include_credentials=True,
        )
        response = cast(
            FinalizeTrackingResponse,
            {
                **result,
                "project_id": resolved_project_id,
                "experiment_id": experiment_id,
                "experiment": experiment,
            },
        )
        if isinstance(run, dict) and run.get("run_id"):
            note = (
                self.dispatcher.dispatch(
                    event=committed.event, phase="post_response", state=state
                ).outcomes.get("feed")
                if committed is not None
                else self._feed_advisory(state)
            )
            if isinstance(note, str):
                response["feed_note"] = note
        return response

    def _feed_advisory(self, state: ExperimentState) -> str | None:
        try:
            return self.feed.transition_advisory(
                project_id=str(state.get("project_id") or ""),
                experiment_id=str(state.get("id") or ""),
                event="mlflow_run_finalized",
            )
        except Exception:  # advisory only
            return None


__all__ = [
    "AgentExperimentQuery",
    "ExperimentDetailResponse",
    "ExperimentDetailQuery",
    "FinalizeTrackingResponse",
    "FinalizeTrackingRun",
    "GetTrackingContext",
    "TrackingContextResponse",
]
