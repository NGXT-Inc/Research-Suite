from __future__ import annotations

import unittest
from copy import deepcopy
from typing import Any, get_type_hints
from unittest.mock import patch

from merv.brain.application.experiments.tracking import (
    AgentExperimentQuery,
    ExperimentDetailResponse,
    ExperimentDetailQuery,
    FinalizeTrackingResponse,
    FinalizeTrackingRun,
    GetTrackingContext,
    TrackingContextResponse,
)
from merv.brain.application.events import EventDispatcher
from merv.brain.application.experiments.reactions import ExperimentReactions
from merv.brain.kernel.events import StoredEvent, freeze_json_object
from merv.brain.research_core.facade import CommittedTrackingRunRefresh


PROJECT_ID = "proj_1"
EXPERIMENT_ID = "exp_1"


def _event() -> StoredEvent:
    return StoredEvent(
        id=73,
        project_id=PROJECT_ID,
        type="experiment.mlflow_run_refreshed",
        target_type="experiment",
        target_id=EXPERIMENT_ID,
        payload=freeze_json_object(
            {
                "run_id": "run_mine",
                "run_name": "owned",
                "status": "FINISHED",
                "error": "",
                "previous_run_id": "run_mine",
            }
        ),
        created_at="2026-07-19T18:00:00Z",
    )


def _state(*, run_id: str = "run_mine", run_status: str = "RUNNING") -> dict[str, Any]:
    return {
        "id": EXPERIMENT_ID,
        "project_id": PROJECT_ID,
        "name": "Tracking Slice",
        "intent": "Preserve the wire contract",
        "status": "running",
        "attempt_index": 1,
        "mlflow_run": {
            "run_id": run_id,
            "run_name": "owned",
            "status": run_status,
            "created_by_plugin": True,
        },
    }


class _Context:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self.payload)


class RecordingResearch:
    def __init__(
        self,
        order: list[str],
        *,
        state: dict[str, Any] | None = None,
        refreshed: dict[str, Any] | None = None,
        refresh_error: Exception | None = None,
    ) -> None:
        self.order = order
        self.state = state or _state()
        self.refreshed = refreshed or _state(run_status="FINISHED")
        self.refresh_error = refresh_error
        self.refresh_calls: list[dict[str, Any]] = []
        self.project_calls: list[str] = []
        self.event = _event()

    def experiment_state(self, **kwargs: Any) -> dict[str, Any]:
        self.order.append("research.state")
        return deepcopy(self.state)

    def project_experiments(self, *, project_id: str) -> list[dict[str, Any]]:
        self.order.append("research.project_experiments")
        self.project_calls.append(project_id)
        return [deepcopy(self.state)]

    def refresh_tracking_run(self, **kwargs: Any) -> CommittedTrackingRunRefresh:
        self.order.append("research.refresh")
        self.refresh_calls.append(deepcopy(kwargs))
        if self.refresh_error is not None:
            raise self.refresh_error
        return CommittedTrackingRunRefresh(
            state=deepcopy(self.refreshed), event=self.event
        )


class RecordingTracking:
    def __init__(
        self,
        order: list[str],
        *,
        finalize_result: dict[str, Any] | None = None,
        finalize_error: Exception | None = None,
    ) -> None:
        self.order = order
        self.finalize_result = finalize_result or {
            "configured": True,
            "run_id": "run_mine",
            "terminal": True,
            "run": {
                "run_id": "run_mine",
                "run_name": "owned",
                "status": "FINISHED",
            },
        }
        self.finalize_error = finalize_error
        self.context_calls: list[dict[str, Any]] = []
        self.project_context_calls: list[dict[str, Any]] = []
        self.finalize_calls: list[dict[str, Any]] = []

    def context(self, **kwargs: Any) -> _Context:
        self.order.append("tracking.context")
        self.context_calls.append(kwargs)
        return _Context(
            {
                "configured": True,
                "experiment_name": f"merv/{kwargs['project_id']}/{kwargs['experiment_id']}",
                "env": {
                    "MLFLOW_TRACKING_URI": "https://tracking.test",
                    "MLFLOW_TRACKING_PASSWORD": "public-secret",
                },
            }
        )

    def project_context(self, **kwargs: Any) -> dict[str, Any]:
        self.order.append("tracking.project_context")
        self.project_context_calls.append(kwargs)
        return {
            "configured": True,
            "tracking_uri": "https://tracking.test",
            "experiment_namespace_prefix": f"merv/{kwargs['project_id']}/",
            "env": {"MLFLOW_TRACKING_PASSWORD": "public-secret"},
        }

    def finalize_run(self, **kwargs: Any) -> dict[str, Any]:
        self.order.append("tracking.finalize")
        self.finalize_calls.append(kwargs)
        if self.finalize_error is not None:
            raise self.finalize_error
        return deepcopy(self.finalize_result)


class RecordingFeed:
    def __init__(
        self, order: list[str], *, note: str | None = "Share it.", raises: bool = False
    ) -> None:
        self.order = order
        self.note = note
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def transition_advisory(self, **kwargs: Any) -> str | None:
        self.order.append("feed.advisory")
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("feed unavailable")
        return self.note


class RecordingObjects:
    def __init__(
        self,
        order: list[str],
        *,
        error: Exception | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.order = order
        self.error = error
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    def by_experiment(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        self.order.append("objects.by_experiment")
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            experiment_id: deepcopy(self.rows)
            for experiment_id in kwargs["experiment_ids"]
        }


class TrackingContextQueryTest(unittest.TestCase):
    def test_use_cases_expose_typed_public_results(self) -> None:
        self.assertIs(
            get_type_hints(GetTrackingContext.execute)["return"],
            TrackingContextResponse,
        )
        self.assertIs(
            get_type_hints(FinalizeTrackingRun.execute)["return"],
            FinalizeTrackingResponse,
        )
        self.assertIs(
            get_type_hints(ExperimentDetailQuery.__call__)["return"],
            ExperimentDetailResponse,
        )
        self.assertEqual(
            get_type_hints(AgentExperimentQuery.__call__)["return"].__name__,
            "SlimExperimentState",
        )

    def test_project_context_uses_port_and_research_namespace_map(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        tracking = RecordingTracking(order)

        result = GetTrackingContext(research=research, tracking=tracking).execute(
            project_id=PROJECT_ID
        )

        self.assertEqual(result["scope"], "project")
        self.assertEqual(result["project_id"], PROJECT_ID)
        self.assertEqual(
            result["mlflow"]["experiments"],
            [
                {
                    "experiment_id": EXPERIMENT_ID,
                    "name": "Tracking Slice",
                    "status": "running",
                    "intent": "Preserve the wire contract",
                    "mlflow_experiment_name": f"merv/{PROJECT_ID}/{EXPERIMENT_ID}",
                }
            ],
        )
        self.assertEqual(
            tracking.project_context_calls,
            [{"project_id": PROJECT_ID, "include_credentials": True}],
        )
        self.assertEqual(research.project_calls, [PROJECT_ID])
        self.assertEqual(order, ["tracking.project_context", "research.project_experiments"])

    def test_experiment_context_resolves_identity_and_preserves_credentials(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        tracking = RecordingTracking(order)
        query = GetTrackingContext(research=research, tracking=tracking)

        result = query.execute(project_id="caller_project", experiment_id=EXPERIMENT_ID)

        self.assertEqual(result["project_id"], PROJECT_ID)
        self.assertEqual(result["experiment_id"], EXPERIMENT_ID)
        self.assertEqual(result["scope"], "experiment")
        self.assertEqual(result["mlflow"]["run"]["run_id"], "run_mine")
        self.assertEqual(result["mlflow"]["env"]["MLFLOW_RUN_ID"], "run_mine")
        self.assertEqual(
            tracking.context_calls[0],
            {
                "project_id": PROJECT_ID,
                "experiment_id": EXPERIMENT_ID,
                "include_credentials": True,
            },
        )
        self.assertEqual(
            result["mlflow"]["env"]["MLFLOW_TRACKING_PASSWORD"], "public-secret"
        )

    def test_unconfigured_project_context_is_exact_and_does_not_list(self) -> None:
        order: list[str] = []
        result = GetTrackingContext(
            research=RecordingResearch(order), tracking=None
        ).execute(project_id=PROJECT_ID)

        self.assertEqual(result["mlflow"], {"configured": False})
        self.assertEqual(order, [])

    def test_http_detail_uses_application_tracking_policy_without_credentials(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        tracking = RecordingTracking(order)

        result = ExperimentDetailQuery(
            research=research,
            objects=RecordingObjects(order),
            tracking=tracking,
        )(experiment_id=EXPERIMENT_ID, project_id=PROJECT_ID)

        self.assertEqual(result["mlflow"]["run"]["run_id"], "run_mine")
        self.assertEqual(result["mlflow"]["env"]["MLFLOW_RUN_ID"], "run_mine")
        self.assertEqual(
            tracking.context_calls,
            [
                {
                    "project_id": PROJECT_ID,
                    "experiment_id": EXPERIMENT_ID,
                    "include_credentials": False,
                }
            ],
        )

    def test_http_detail_hides_tracking_for_planned_experiment(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order, state={**_state(), "status": "planned"})
        tracking = RecordingTracking(order)

        result = ExperimentDetailQuery(
            research=research,
            objects=RecordingObjects(order),
            tracking=tracking,
        )(experiment_id=EXPERIMENT_ID, project_id=PROJECT_ID)

        self.assertNotIn("mlflow", result)
        self.assertEqual(tracking.context_calls, [])


class FinalizeTrackingRunTest(unittest.TestCase):
    def _command(
        self,
        *,
        research: RecordingResearch,
        tracking: RecordingTracking | None,
        feed: RecordingFeed,
        objects: RecordingObjects | None = None,
    ) -> FinalizeTrackingRun:
        dispatcher = EventDispatcher()
        ExperimentReactions(
            research=research, tracking=tracking, feed=feed
        ).bind(dispatcher)
        return FinalizeTrackingRun(
            research=research,
            tracking=tracking,
            feed=feed,
            dispatcher=dispatcher,
            objects=objects or RecordingObjects(research.order),
        )

    def test_unconfigured_response_remains_exact(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        feed = RecordingFeed(order)

        result = self._command(research=research, tracking=None, feed=feed).execute(
            project_id=PROJECT_ID, experiment_id=EXPERIMENT_ID
        )

        self.assertEqual(
            result,
            {
                "project_id": PROJECT_ID,
                "experiment_id": EXPERIMENT_ID,
                "configured": False,
                "run_id": "run_mine",
                "error": "MLflow tracking is not configured on this backend.",
            },
        )
        self.assertEqual(order, ["research.state"])

    def test_canonical_refresh_dispatches_the_exact_committed_event_after_response(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        tracking = RecordingTracking(order)
        feed = RecordingFeed(order)
        command = self._command(research=research, tracking=tracking, feed=feed)

        with patch.object(
            command.dispatcher, "dispatch", wraps=command.dispatcher.dispatch
        ) as dispatch:
            result = command.execute(
                project_id=PROJECT_ID,
                experiment_id=EXPERIMENT_ID,
                status="FINISHED",
                wait_seconds=3.5,
            )

        self.assertEqual(
            tracking.finalize_calls,
            [
                {
                    "project_id": PROJECT_ID,
                    "experiment_id": EXPERIMENT_ID,
                    "run_id": "run_mine",
                    "status": "FINISHED",
                    "wait_seconds": 3.5,
                }
            ],
        )
        self.assertIs(dispatch.call_args.kwargs["event"], research.event)
        self.assertEqual(dispatch.call_args.kwargs["state"], research.refreshed)
        self.assertEqual(dispatch.call_args.kwargs["phase"], "post_response")
        self.assertEqual(research.refresh_calls[0]["run"]["status"], "FINISHED")
        self.assertEqual(result["run"]["status"], "FINISHED")
        self.assertEqual(result["experiment"]["mlflow_run"]["status"], "FINISHED")
        self.assertEqual(result["experiment"]["mlflow"]["run"]["status"], "FINISHED")
        self.assertEqual(result["feed_note"], "Share it.")
        self.assertEqual(
            feed.calls,
            [
                {
                    "project_id": PROJECT_ID,
                    "experiment_id": EXPERIMENT_ID,
                    "event": "mlflow_run_finalized",
                }
            ],
        )
        self.assertEqual(
            order,
            [
                "research.state",
                "objects.by_experiment",
                "tracking.finalize",
                "research.refresh",
                "tracking.context",
                "feed.advisory",
            ],
        )

    def test_catalog_failure_prevents_tracking_and_research_side_effects(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        tracking = RecordingTracking(order)
        feed = RecordingFeed(order)
        command = self._command(
            research=research,
            tracking=tracking,
            feed=feed,
            objects=RecordingObjects(order, error=RuntimeError("catalog down")),
        )

        with self.assertRaisesRegex(RuntimeError, "catalog down"):
            command.execute(project_id=PROJECT_ID, experiment_id=EXPERIMENT_ID)

        self.assertEqual(order, ["research.state", "objects.by_experiment"])
        self.assertEqual(tracking.finalize_calls, [])
        self.assertEqual(research.refresh_calls, [])
        self.assertEqual(feed.calls, [])

    def test_finalize_response_retains_produced_objects(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        tracking = RecordingTracking(order)
        feed = RecordingFeed(order)
        command = self._command(
            research=research,
            tracking=tracking,
            feed=feed,
            objects=RecordingObjects(
                order,
                rows=[
                    {
                        "id": "so_1",
                        "name": "models/checkpoint.bin",
                        "kind": "model",
                        "status": "available",
                    }
                ],
            ),
        )

        result = command.execute(
            project_id=PROJECT_ID, experiment_id=EXPERIMENT_ID
        )

        self.assertEqual(result["experiment"]["storage_objects"][0]["id"], "so_1")

    def test_foreign_readback_keeps_canonical_identity_and_current_feed_response(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        tracking = RecordingTracking(
            order,
            finalize_result={
                "configured": True,
                "terminal": True,
                "run": {"run_id": "run_foreign", "status": "FINISHED"},
            },
        )
        feed = RecordingFeed(order)
        command = self._command(research=research, tracking=tracking, feed=feed)

        with patch.object(command.dispatcher, "dispatch") as dispatch:
            result = command.execute(
                project_id=PROJECT_ID,
                experiment_id=EXPERIMENT_ID,
                run_id="run_foreign",
            )

        dispatch.assert_not_called()
        self.assertEqual(research.refresh_calls, [])
        self.assertEqual(result["run"]["run_id"], "run_foreign")
        self.assertEqual(result["experiment"]["mlflow_run"]["run_id"], "run_mine")
        self.assertEqual(result["feed_note"], "Share it.")

    def test_no_readback_run_does_not_persist_or_query_feed(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        tracking = RecordingTracking(order, finalize_result={"error": "not found"})
        feed = RecordingFeed(order)

        result = self._command(
            research=research, tracking=tracking, feed=feed
        ).execute(project_id=PROJECT_ID, experiment_id=EXPERIMENT_ID)

        self.assertEqual(result["error"], "not found")
        self.assertEqual(research.refresh_calls, [])
        self.assertEqual(feed.calls, [])

    def test_feed_failure_is_advisory(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        result = self._command(
            research=research,
            tracking=RecordingTracking(order),
            feed=RecordingFeed(order, raises=True),
        ).execute(project_id=PROJECT_ID, experiment_id=EXPERIMENT_ID)

        self.assertNotIn("feed_note", result)
        self.assertEqual(result["experiment"]["mlflow_run"]["status"], "FINISHED")

    def test_refresh_failure_propagates_before_presentation_or_feed(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order, refresh_error=RuntimeError("db down"))
        command = self._command(
            research=research,
            tracking=RecordingTracking(order),
            feed=RecordingFeed(order),
        )

        with self.assertRaisesRegex(RuntimeError, "db down"):
            command.execute(project_id=PROJECT_ID, experiment_id=EXPERIMENT_ID)

        self.assertEqual(
            order,
            [
                "research.state",
                "objects.by_experiment",
                "tracking.finalize",
                "research.refresh",
            ],
        )

    def test_tracking_failure_propagates_without_persistence_or_feed(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        command = self._command(
            research=research,
            tracking=RecordingTracking(order, finalize_error=RuntimeError("tracking down")),
            feed=RecordingFeed(order),
        )

        with self.assertRaisesRegex(RuntimeError, "tracking down"):
            command.execute(project_id=PROJECT_ID, experiment_id=EXPERIMENT_ID)

        self.assertEqual(
            order,
            ["research.state", "objects.by_experiment", "tracking.finalize"],
        )


if __name__ == "__main__":
    unittest.main()
