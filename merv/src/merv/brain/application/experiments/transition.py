"""The experiment-transition application command and its event reactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from merv.shared.artifact_roles import EXHIBIT_ROLE

from ...artifacts.facade import Artifacts
from ...research_core.facade import (
    ExperimentState,
    ResearchCore,
)
from ..events import EventDispatcher
from ..ports.storage import ProducedObjectCatalog
from ..ports.tracking import ExperimentTracking, TrackingContextPayload
from .exhibits import ExhibitBuilder, should_pin_exhibit
from .metrics_exhibit import METRICS_EXHIBIT_FILENAME, exhibit_bytes
from .presentation import SlimExperimentState, slim_experiment_state
from .tracking_presentation import with_tracking_if_visible


class TransitionResponse(SlimExperimentState, total=False):
    mlflow: TrackingContextPayload
    mlflow_guidance: str
    metrics_exhibit: dict[str, object]
    feed_note: str


@dataclass(kw_only=True, eq=False, repr=False)
class TransitionExperiment:
    """Coordinate one transition without exposing component internals."""

    research: ResearchCore
    artifacts: Artifacts
    tracking: ExperimentTracking | None
    exhibits: ExhibitBuilder
    dispatcher: EventDispatcher
    objects: ProducedObjectCatalog

    def execute(
        self,
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, Any] | None = None,
        project_id: str | None = None,
        include_tracking_credentials: bool = False,
    ) -> TransitionResponse:
        before = (
            self.research.experiment_state(
                experiment_id=experiment_id, project_id=project_id
            )
            if transition == "submit_results" or not project_id
            else None
        )
        resolved_project_id = str((before or {}).get("project_id") or project_id or "")
        storage_objects = self.objects.by_experiment(
            project_id=resolved_project_id, experiment_ids=(experiment_id,)
        )[experiment_id]
        exhibit = None
        if (
            transition == "submit_results"
            and before is not None
            and str(before.get("status")) == "running"
        ):
            exhibit = self._finalize_exhibit(state=before)

        committed = self.research.transition_experiment(
            experiment_id=experiment_id,
            transition=transition,
            evidence=evidence,
            project_id=project_id,
        )
        reacted = self.dispatcher.dispatch(
            event=committed.event, phase="post_commit", state=committed.state
        )
        state = reacted.state
        response = cast(
            TransitionResponse,
            dict(
                slim_experiment_state(state, storage_objects=storage_objects)
            ),
        )
        with_tracking_if_visible(
            state=response,
            tracking=self.tracking,
            project_id=resolved_project_id,
            experiment_id=experiment_id,
            include_credentials=include_tracking_credentials,
        )
        if transition in ("start_running", "retry_running"):
            response["metrics_exhibit"] = self._exhibit_expectation(
                experiment_id=experiment_id, state=response
            )
        elif transition == "submit_results" and exhibit is not None:
            response["metrics_exhibit"] = {
                "pinned": True,
                "path": self._exhibit_path(experiment_id=experiment_id, state=response),
                "verdict": exhibit["verdict"],
            }

        late = self.dispatcher.dispatch(
            event=committed.event, phase="post_response", state=state
        )
        note = late.outcomes.get("feed")
        if isinstance(note, str):
            response["feed_note"] = note
        return response

    def _finalize_exhibit(
        self, *, state: ExperimentState
    ) -> dict[str, object] | None:
        exhibit = self.exhibits.generate(state=state)
        pinned = should_pin_exhibit(exhibit=exhibit, state=state)
        verdict = {
            **dict(exhibit["verdict"]),
            "attempt_index": exhibit["attempt_index"],
            "mlflow": exhibit["mlflow"],
            "pinned": pinned,
        }
        project_id = str(state.get("project_id") or "")
        experiment_id = str(state.get("id") or "")
        self.research.record_exhibit_verdict(
            experiment_id=experiment_id,
            project_id=project_id,
            verdict=verdict,
        )
        if not pinned:
            return None
        self.artifacts.pin_system_artifact(
            path=self._exhibit_path(experiment_id=experiment_id, state=state),
            experiment_id=experiment_id,
            role=EXHIBIT_ROLE,
            content_bytes=exhibit_bytes(exhibit),
            content_type="application/json",
            title="Metrics exhibit (system-generated)",
            kind="result",
            project_id=project_id,
        )
        return exhibit

    def _exhibit_path(
        self, *, experiment_id: str, state: dict[str, Any]
    ) -> str:
        return self.research.exhibit_path(
            experiment_id=experiment_id,
            name=str(state.get("name") or ""),
            filename=METRICS_EXHIBIT_FILENAME,
        )

    def _exhibit_expectation(
        self, *, experiment_id: str, state: dict[str, Any]
    ) -> dict[str, object]:
        path = self._exhibit_path(experiment_id=experiment_id, state=state)
        return {
            "final_path": path,
            "preview_tool": "experiment.exhibit",
            "notice": (
                "At submit_results the system generates a metrics exhibit from "
                "up to the newest 50 MLflow runs in this attempt's window (no "
                "curation; the cap is recorded) and eligible pinned result JSON "
                "(metrics.json, results.json, and results/*.json associated with "
                "role 'result'). It pins the exhibit when matching runs are found, "
                "or when MLflow is unavailable after a plugin-created run, at "
                f"{path}. When pinned, your report must reference "
                f"{METRICS_EXHIBIT_FILENAME} and answer around it — log every run "
                "to the MLflow env you were handed, tag project_id/experiment_id, "
                "and pull result files before submitting. Preview anytime with "
                "experiment.exhibit; later runs remain in MLflow but are outside "
                "the finalized exhibit."
            ),
        }

__all__ = ["TransitionExperiment", "TransitionResponse"]
