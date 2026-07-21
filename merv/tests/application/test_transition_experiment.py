from __future__ import annotations

import json
import unittest
from copy import deepcopy
from typing import Any

from merv.brain.application.events import EventDispatcher
from merv.brain.application.experiments.reactions import ExperimentReactions
from merv.brain.application.experiments.transition import TransitionExperiment
from merv.brain.application.ports.tracking import TrackingCapabilities
from merv.brain.kernel.events import StoredEvent, freeze_json_object
from merv.brain.research_core.transition_types import CommittedExperimentTransition


PROJECT_ID = "proj_1"
EXPERIMENT_ID = "exp_1"
CREATED_AT = "2026-07-19T12:34:56.789000Z"
_MISSING = object()


def _event(
    transition: str,
    *,
    event_type: str = "experiment.transitioned",
    payload_status: str = "intentionally-not-the-state",
) -> StoredEvent:
    return StoredEvent(
        id=41,
        project_id=PROJECT_ID,
        type=event_type,
        target_type="experiment",
        target_id=EXPERIMENT_ID,
        payload=freeze_json_object(
            {
                "evidence": {"source": "characterization"},
                "from": "ready_to_run",
                "status": payload_status,
                "transition": transition,
            }
        ),
        created_at=CREATED_AT,
    )


def _state(
    status: str,
    *,
    attempt_index: int = 3,
    run: object = _MISSING,
    token: str = "committed",
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "id": EXPERIMENT_ID,
        "project_id": PROJECT_ID,
        "name": "A Characterized Experiment",
        "status": status,
        "attempt_index": attempt_index,
        "state_token": token,
    }
    if run is not _MISSING:
        state["mlflow_run"] = run
    return state


def _open_run(run_id: str = "run_open") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "run_name": f"{EXPERIMENT_ID}-attempt-3",
        "status": "RUNNING",
        "artifact_uri": f"s3://tracking/{run_id}",
        "created_at": "2026-07-19T12:00:00Z",
        "created_by_plugin": True,
    }


def _created_run(run_id: str = "run_new") -> dict[str, Any]:
    return {
        "created": True,
        "configured": True,
        "control_configured": True,
        "experiment_name": f"merv/{PROJECT_ID}/{EXPERIMENT_ID}",
        "experiment_id": "tracking-exp-7",
        "run_id": run_id,
        "run_name": f"{EXPERIMENT_ID}-attempt-3",
        "status": "RUNNING",
        "artifact_uri": f"s3://tracking/{run_id}",
        "created_at": "2026-07-19T12:35:00Z",
        "dashboard_run_url": f"https://tracking.test/runs/{run_id}",
    }


def _exhibit(*, runs_found: int = 1) -> dict[str, Any]:
    return {
        "kind": "metrics_exhibit",
        "project_id": PROJECT_ID,
        "experiment_id": EXPERIMENT_ID,
        "attempt_index": 3,
        "window": {"started_at": "2026-07-19T12:00:00Z"},
        "mlflow": {
            "configured": True,
            "available": True,
            "experiment_name": f"merv/{PROJECT_ID}/{EXPERIMENT_ID}",
            "runs_excluded_by_window": 0,
        },
        "runs": ([{"run_id": "run_open"}] if runs_found else []),
        "result_files": [],
        "verdict": {"runs_found": runs_found, "result_files": 0},
    }


class _Context:
    def __init__(
        self,
        *,
        payload: dict[str, Any],
        order: list[str],
        serialization_error: Exception | None,
    ) -> None:
        self._payload = payload
        self._order = order
        self._serialization_error = serialization_error

    @property
    def configured(self) -> bool:
        return bool(self._payload.get("configured"))

    @property
    def experiment_name(self) -> str:
        return str(self._payload.get("experiment_name") or "")

    def to_dict(self) -> dict[str, Any]:
        self._order.append("tracking.context.serialize")
        if self._serialization_error is not None:
            raise self._serialization_error
        return deepcopy(self._payload)


class RecordingTracking:
    def __init__(
        self,
        order: list[str],
        *,
        create_result: dict[str, Any] | None = None,
        finalize_result: dict[str, Any] | None = None,
        finalize_error: Exception | None = None,
        context_error: Exception | None = None,
        capabilities: TrackingCapabilities | None = None,
    ) -> None:
        self.order = order
        self.create_result = create_result or {}
        self.finalize_result = finalize_result or {}
        self.finalize_error = finalize_error
        self.context_error = context_error
        self._capabilities = capabilities or TrackingCapabilities(
            logging=True, control=True, readback=True
        )
        self.create_calls: list[dict[str, Any]] = []
        self.finalize_calls: list[dict[str, Any]] = []
        self.context_calls: list[dict[str, Any]] = []

    def capabilities(self) -> TrackingCapabilities:
        self.order.append("tracking.capabilities")
        return self._capabilities

    def context(
        self,
        *,
        project_id: str,
        experiment_id: str,
        include_credentials: bool = False,
    ) -> _Context:
        self.order.append("tracking.context")
        call = {
            "project_id": project_id,
            "experiment_id": experiment_id,
            "include_credentials": include_credentials,
        }
        self.context_calls.append(call)
        env = {
            "MLFLOW_TRACKING_URI": "https://tracking.test",
            "MLFLOW_EXPERIMENT_NAME": f"merv/{project_id}/{experiment_id}",
        }
        if include_credentials:
            env["MLFLOW_TRACKING_PASSWORD"] = "credential-for-public-response"
        return _Context(
            payload={
                "configured": True,
                "experiment_name": f"merv/{project_id}/{experiment_id}",
                "env": env,
            },
            order=self.order,
            serialization_error=self.context_error,
        )

    def create_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        attempt_index: int,
        run_name: str,
    ) -> dict[str, Any]:
        self.order.append("tracking.create")
        call = {
            "project_id": project_id,
            "experiment_id": experiment_id,
            "attempt_index": attempt_index,
            "run_name": run_name,
        }
        self.create_calls.append(call)
        return deepcopy(self.create_result)

    def finalize_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run_id: str,
        status: str,
        wait_seconds: float,
    ) -> dict[str, Any]:
        self.order.append("tracking.finalize")
        call = {
            "project_id": project_id,
            "experiment_id": experiment_id,
            "run_id": run_id,
            "status": status,
            "wait_seconds": wait_seconds,
        }
        self.finalize_calls.append(call)
        if self.finalize_error is not None:
            raise self.finalize_error
        return deepcopy(self.finalize_result)

    def results_metrics(
        self, *, project_id: str, experiment_id: str
    ) -> dict[str, Any]:  # pragma: no cover - the exhibit collaborator owns this read
        raise AssertionError("TransitionExperiment must use its exhibit collaborator")


class RecordingResearch:
    def __init__(
        self,
        order: list[str],
        *,
        before: dict[str, Any] | None = None,
        committed: dict[str, Any] | None = None,
        event: StoredEvent | None = None,
        persisted: dict[str, Any] | None = None,
        transition_error: Exception | None = None,
        persistence_error: Exception | None = None,
    ) -> None:
        self.order = order
        self.before = before or _state("running", run=_open_run(), token="before")
        self.committed = committed or _state("running", run=_open_run())
        self.event = event or _event("start_running")
        self.persisted = persisted
        self.transition_error = transition_error
        self.persistence_error = persistence_error
        self.transition_committed = False
        self.transition_calls: list[dict[str, Any]] = []
        self.persist_calls: list[dict[str, Any]] = []
        self.verdicts: list[dict[str, Any]] = []

    def experiment_state(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        self.order.append("research.state")
        return self.before

    def transition_experiment(
        self,
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, object] | None = None,
        project_id: str | None = None,
    ) -> CommittedExperimentTransition:
        self.order.append("research.transition")
        self.transition_calls.append(
            {
                "experiment_id": experiment_id,
                "transition": transition,
                "evidence": evidence,
                "project_id": project_id,
            }
        )
        if self.transition_error is not None:
            raise self.transition_error
        self.transition_committed = True
        return CommittedExperimentTransition(state=self.committed, event=self.event)

    def record_tracking_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run: dict[str, Any],
        event_type: str | None = None,
    ) -> dict[str, Any]:
        self.order.append("research.record_tracking")
        self.persist_calls.append(
            {
                "project_id": project_id,
                "experiment_id": experiment_id,
                "run": deepcopy(run),
                "event_type": event_type,
            }
        )
        if self.persistence_error is not None:
            raise self.persistence_error
        if self.persisted is not None:
            return self.persisted
        state = dict(self.committed)
        state["mlflow_run"] = deepcopy(run)
        return state

    def record_exhibit_verdict(
        self,
        *,
        experiment_id: str,
        project_id: str,
        verdict: dict[str, Any],
    ) -> None:
        self.order.append("research.exhibit_verdict")
        self.verdicts.append(deepcopy(verdict))

    def attempt_started_running_at(self, *, experiment_id: str) -> str | None:
        return "2026-07-19T12:00:00Z"

    def exhibit_path(self, *, experiment_id: str, name: str, filename: str) -> str:
        return f"experiments/a-characterized-experiment/{filename}"


class RecordingArtifacts:
    def __init__(
        self, order: list[str], *, pin_error: Exception | None = None
    ) -> None:
        self.order = order
        self.pin_error = pin_error
        self.pin_attempts: list[dict[str, Any]] = []
        self.pins: list[dict[str, Any]] = []

    def metric_file_sources(
        self, *, experiment_id: str, attempt_index: int
    ) -> list[dict[str, Any]]:  # pragma: no cover - collaborator owns generation
        raise AssertionError("TransitionExperiment must use its exhibit collaborator")

    def pin_system_artifact(self, **kwargs: Any) -> None:
        self.order.append("artifacts.pin")
        copied = deepcopy(kwargs)
        self.pin_attempts.append(copied)
        if self.pin_error is not None:
            raise self.pin_error
        self.pins.append(copied)


class RecordingFeed:
    def __init__(
        self,
        order: list[str],
        *,
        note: str | None = "A terminal update is ready for the feed.",
        error: Exception | None = None,
    ) -> None:
        self.order = order
        self.note = note
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def transition_advisory(
        self, *, project_id: str, experiment_id: str, event: str
    ) -> str | None:
        self.order.append("feed.advisory")
        self.calls.append(
            {
                "project_id": project_id,
                "experiment_id": experiment_id,
                "event": event,
            }
        )
        if self.error is not None:
            raise self.error
        return self.note


class RecordingExhibits:
    def __init__(
        self,
        order: list[str],
        *,
        exhibit: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.order = order
        self.exhibit = exhibit or _exhibit()
        self.error = error
        self.states: list[dict[str, Any]] = []

    def generate(self, *, state: dict[str, Any]) -> dict[str, Any]:
        self.order.append("exhibits.generate")
        self.states.append(state)
        if self.error is not None:
            raise self.error
        return deepcopy(self.exhibit)


class RecordingObjects:
    def __init__(
        self, order: list[str], *, error: Exception | None = None
    ) -> None:
        self.order = order
        self.error = error

    def by_experiment(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        self.order.append("objects.by_experiment")
        if self.error is not None:
            raise self.error
        return {experiment_id: [] for experiment_id in kwargs["experiment_ids"]}


def _use_case(
    *,
    research: RecordingResearch,
    artifacts: RecordingArtifacts,
    feed: RecordingFeed,
    tracking: RecordingTracking | None,
    exhibits: RecordingExhibits,
    objects: RecordingObjects | None = None,
) -> TransitionExperiment:
    dispatcher = EventDispatcher()
    ExperimentReactions(
        research=research, feed=feed, tracking=tracking
    ).bind(dispatcher)
    return TransitionExperiment(
        research=research,
        artifacts=artifacts,
        tracking=tracking,
        exhibits=exhibits,
        dispatcher=dispatcher,
        objects=objects or RecordingObjects(research.order),
    )


class TransitionStoragePrefetchTest(unittest.TestCase):
    def test_catalog_failure_prevents_exhibit_and_transition_side_effects(self) -> None:
        order: list[str] = []
        research = RecordingResearch(order)
        artifacts = RecordingArtifacts(order)
        feed = RecordingFeed(order)
        tracking = RecordingTracking(order)
        exhibits = RecordingExhibits(order)
        use_case = _use_case(
            research=research,
            artifacts=artifacts,
            feed=feed,
            tracking=tracking,
            exhibits=exhibits,
            objects=RecordingObjects(order, error=RuntimeError("catalog down")),
        )

        with self.assertRaisesRegex(RuntimeError, "catalog down"):
            use_case.execute(
                experiment_id=EXPERIMENT_ID,
                transition="submit_results",
                project_id=PROJECT_ID,
            )

        self.assertEqual(order, ["research.state", "objects.by_experiment"])
        self.assertEqual(research.verdicts, [])
        self.assertEqual(research.transition_calls, [])
        self.assertEqual(exhibits.states, [])
        self.assertEqual(artifacts.pin_attempts, [])
        self.assertEqual(tracking.create_calls, [])
        self.assertEqual(feed.calls, [])


class StartAndRetryTransitionTest(unittest.TestCase):
    def _fixture(
        self,
        *,
        committed: dict[str, Any],
        event: StoredEvent,
        persisted: dict[str, Any] | None = None,
        create_result: dict[str, Any] | None = None,
        persistence_error: Exception | None = None,
    ) -> tuple[
        TransitionExperiment,
        RecordingResearch,
        RecordingTracking,
        RecordingFeed,
        list[str],
    ]:
        order: list[str] = []
        research = RecordingResearch(
            order,
            committed=committed,
            event=event,
            persisted=persisted,
            persistence_error=persistence_error,
        )
        tracking = RecordingTracking(order, create_result=create_result)
        feed = RecordingFeed(order)
        use_case = _use_case(
            research=research,
            artifacts=RecordingArtifacts(order),
            feed=feed,
            tracking=tracking,
            exhibits=RecordingExhibits(order),
        )
        return use_case, research, tracking, feed, order

    def test_start_creates_persists_and_threads_the_exact_returned_state(self) -> None:
        committed = _state("running", run=None)
        persisted = _state("running", run=_open_run("run_new"), token="persisted")
        event = _event("start_running")
        use_case, research, tracking, _feed, order = self._fixture(
            committed=committed,
            event=event,
            persisted=persisted,
            create_result=_created_run(),
        )

        result = use_case.execute(
            experiment_id=EXPERIMENT_ID,
            transition="start_running",
            evidence={"reason": "ready"},
            project_id=PROJECT_ID,
            include_tracking_credentials=True,
        )

        self.assertTrue(research.transition_committed)
        self.assertIs(research.event, event)
        self.assertEqual(
            tracking.create_calls,
            [
                {
                    "project_id": PROJECT_ID,
                    "experiment_id": EXPERIMENT_ID,
                    "attempt_index": 3,
                    "run_name": f"{EXPERIMENT_ID}-attempt-3",
                }
            ],
        )
        persisted_run = research.persist_calls[0]["run"]
        self.assertEqual(persisted_run["run_id"], "run_new")
        self.assertEqual(persisted_run["status"], "RUNNING")
        self.assertTrue(persisted_run["created_by_plugin"])
        self.assertNotIn("configured", persisted_run)
        self.assertNotIn("dashboard_run_url", persisted_run)
        self.assertEqual(result["mlflow_run"]["run_id"], "run_new")
        self.assertEqual(result["mlflow"]["run"]["run_id"], "run_new")
        self.assertEqual(result["mlflow"]["env"]["MLFLOW_RUN_ID"], "run_new")
        self.assertIn("metrics_exhibit", result)
        self.assertEqual(
            [
                item
                for item in order
                if item
                in {
                    "research.transition",
                    "tracking.create",
                    "research.record_tracking",
                    "tracking.context.serialize",
                }
            ],
            [
                "research.transition",
                "tracking.create",
                "research.record_tracking",
                "tracking.context.serialize",
            ],
        )
        self.assertEqual(
            order[:2],
            ["objects.by_experiment", "research.transition"],
        )

    def test_start_reuses_an_existing_run_and_retains_exact_transition_state(self) -> None:
        committed = _state("running", run=_open_run())
        use_case, research, tracking, _feed, _order = self._fixture(
            committed=committed,
            event=_event("start_running"),
        )

        result = use_case.execute(
            experiment_id=EXPERIMENT_ID,
            transition="start_running",
            project_id=PROJECT_ID,
        )

        self.assertEqual(tracking.create_calls, [])
        self.assertEqual(research.persist_calls, [])
        self.assertEqual(result["mlflow_run"]["run_id"], "run_open")

    def test_start_persists_a_normalized_adapter_error(self) -> None:
        committed = _state("running", run=None)
        persisted = _state(
            "running",
            run={
                "run_id": None,
                "run_name": f"{EXPERIMENT_ID}-attempt-3",
                "status": "",
                "error": "tracking control plane unavailable",
            },
            token="persisted-error",
        )
        use_case, research, _tracking, _feed, _order = self._fixture(
            committed=committed,
            persisted=persisted,
            event=_event("start_running"),
            create_result={
                "created": False,
                "configured": True,
                "control_configured": True,
                "run_name": f"{EXPERIMENT_ID}-attempt-3",
                "error": "tracking control plane unavailable",
            },
        )

        result = use_case.execute(
            experiment_id=EXPERIMENT_ID,
            transition="start_running",
            project_id=PROJECT_ID,
        )

        self.assertEqual(
            research.persist_calls[0]["run"]["error"],
            "tracking control plane unavailable",
        )
        self.assertNotIn("configured", research.persist_calls[0]["run"])
        self.assertEqual(
            result["mlflow_run"]["error"], "tracking control plane unavailable"
        )

    def test_start_persistence_failure_propagates_after_commit_and_skips_response_phase(self) -> None:
        failure = RuntimeError("tracking persistence failed")
        use_case, research, tracking, feed, order = self._fixture(
            committed=_state("running", run=None),
            event=_event("start_running"),
            create_result=_created_run(),
            persistence_error=failure,
        )

        with self.assertRaisesRegex(RuntimeError, "tracking persistence failed"):
            use_case.execute(
                experiment_id=EXPERIMENT_ID,
                transition="start_running",
                project_id=PROJECT_ID,
            )

        self.assertTrue(research.transition_committed)
        self.assertEqual(tracking.create_calls[0]["attempt_index"], 3)
        self.assertEqual(tracking.context_calls, [])
        self.assertEqual(feed.calls, [])
        self.assertNotIn("feed.advisory", order)

    def test_retry_reuses_open_run_but_replaces_terminal_run_for_same_attempt(self) -> None:
        cases = (
            (_open_run("run_open"), False, "run_open"),
            ({**_open_run("run_failed"), "status": "FAILED"}, True, "run_retry"),
        )
        for existing, creates, expected_run_id in cases:
            with self.subTest(status=existing["status"]):
                committed = _state("running", attempt_index=3, run=existing)
                persisted = _state(
                    "running",
                    attempt_index=3,
                    run=_open_run("run_retry"),
                    token="persisted",
                )
                use_case, research, tracking, _feed, _order = self._fixture(
                    committed=committed,
                    persisted=persisted,
                    event=_event("retry_running"),
                    create_result=_created_run("run_retry"),
                )

                result = use_case.execute(
                    experiment_id=EXPERIMENT_ID,
                    transition="retry_running",
                    project_id=PROJECT_ID,
                )

                self.assertEqual(bool(tracking.create_calls), creates)
                self.assertEqual(bool(research.persist_calls), creates)
                if creates:
                    self.assertEqual(tracking.create_calls[0]["attempt_index"], 3)
                    self.assertEqual(
                        tracking.create_calls[0]["run_name"],
                        f"{EXPERIMENT_ID}-attempt-3",
                    )
                self.assertEqual(result["mlflow_run"]["run_id"], expected_run_id)

    def test_reactions_are_driven_by_the_exact_returned_event(self) -> None:
        event = _event(
            "start_running",
            event_type="experiment.characterization_only",
            payload_status="complete",
        )
        committed = _state("running", run=None)
        use_case, research, tracking, feed, _order = self._fixture(
            committed=committed,
            event=event,
            create_result=_created_run(),
        )

        result = use_case.execute(
            experiment_id=EXPERIMENT_ID,
            transition="start_running",
            project_id=PROJECT_ID,
        )

        self.assertIs(research.event, event)
        self.assertEqual(event.id, 41)
        self.assertEqual(event.created_at, CREATED_AT)
        self.assertEqual(tracking.create_calls, [])
        self.assertEqual(research.persist_calls, [])
        self.assertEqual(feed.calls, [])
        self.assertIsNone(result.get("mlflow_run"))


class TerminalTrackingTransitionTest(unittest.TestCase):
    def test_terminal_transition_status_mapping_and_exact_persisted_state(self) -> None:
        cases = (
            ("submit_results", "experiment_review", "FINISHED"),
            ("complete", "complete", "FINISHED"),
            ("abandon", "abandoned", "KILLED"),
            ("mark_failed", "failed", "FAILED"),
        )
        for transition, experiment_status, tracking_status in cases:
            with self.subTest(transition=transition):
                order: list[str] = []
                run = _open_run()
                committed = _state(experiment_status, run=run)
                persisted = _state(
                    experiment_status,
                    run={**run, "status": tracking_status},
                    token="persisted-finalization",
                )
                research = RecordingResearch(
                    order,
                    before=_state("running", run=run, token="before"),
                    committed=committed,
                    persisted=persisted,
                    event=_event(transition),
                )
                tracking = RecordingTracking(
                    order,
                    finalize_result={
                        "configured": True,
                        "terminal": True,
                        "run": {**run, "status": tracking_status},
                    },
                )
                use_case = _use_case(
                    research=research,
                    artifacts=RecordingArtifacts(order),
                    feed=RecordingFeed(order, note=None),
                    tracking=tracking,
                    exhibits=RecordingExhibits(order, exhibit=_exhibit(runs_found=0)),
                )

                result = use_case.execute(
                    experiment_id=EXPERIMENT_ID,
                    transition=transition,
                    project_id=PROJECT_ID,
                )

                self.assertEqual(
                    tracking.finalize_calls,
                    [
                        {
                            "project_id": PROJECT_ID,
                            "experiment_id": EXPERIMENT_ID,
                            "run_id": "run_open",
                            "status": tracking_status,
                            "wait_seconds": 0.0,
                        }
                    ],
                )
                self.assertEqual(
                    research.persist_calls[0]["event_type"],
                    "experiment.mlflow_run_refreshed",
                )
                self.assertEqual(result["mlflow_run"]["status"], tracking_status)

    def test_terminal_adapter_and_persistence_failures_are_suppressed_and_feed_runs(self) -> None:
        failures = ("adapter", "persistence")
        for failure_kind in failures:
            with self.subTest(failure=failure_kind):
                order: list[str] = []
                committed = _state("complete", run=_open_run())
                research = RecordingResearch(
                    order,
                    committed=committed,
                    event=_event("complete"),
                    persistence_error=(
                        RuntimeError("persist failed")
                        if failure_kind == "persistence"
                        else None
                    ),
                )
                tracking = RecordingTracking(
                    order,
                    finalize_error=(
                        RuntimeError("adapter failed")
                        if failure_kind == "adapter"
                        else None
                    ),
                    finalize_result={
                        "run": {**_open_run(), "status": "FINISHED"}
                    },
                )
                feed = RecordingFeed(order)
                use_case = _use_case(
                    research=research,
                    artifacts=RecordingArtifacts(order),
                    feed=feed,
                    tracking=tracking,
                    exhibits=RecordingExhibits(order),
                )

                result = use_case.execute(
                    experiment_id=EXPERIMENT_ID,
                    transition="complete",
                    project_id=PROJECT_ID,
                )

                self.assertTrue(research.transition_committed)
                self.assertEqual(result["mlflow_run"]["status"], "RUNNING")
                self.assertEqual(feed.calls[0]["event"], "experiment_complete")

    def test_no_finalize_for_missing_non_plugin_or_already_terminal_run(self) -> None:
        cases = (
            None,
            {**_open_run(), "created_by_plugin": False},
            {**_open_run(), "status": "FINISHED"},
        )
        for run in cases:
            with self.subTest(run=run):
                order: list[str] = []
                committed = _state("complete", run=run)
                research = RecordingResearch(
                    order, committed=committed, event=_event("complete")
                )
                tracking = RecordingTracking(order)
                use_case = _use_case(
                    research=research,
                    artifacts=RecordingArtifacts(order),
                    feed=RecordingFeed(order, note=None),
                    tracking=tracking,
                    exhibits=RecordingExhibits(order),
                )

                use_case.execute(
                    experiment_id=EXPERIMENT_ID,
                    transition="complete",
                    project_id=PROJECT_ID,
                )

                self.assertEqual(tracking.finalize_calls, [])
                self.assertEqual(research.persist_calls, [])


class FeedTransitionReactionTest(unittest.TestCase):
    def _execute(
        self,
        *,
        status: str,
        transition: str,
        event: StoredEvent | None = None,
        feed_note: str | None = "feed note",
        feed_error: Exception | None = None,
        tracking: RecordingTracking | None = None,
        research: RecordingResearch | None = None,
    ) -> tuple[dict[str, Any], RecordingFeed, RecordingResearch, list[str]]:
        order = tracking.order if tracking is not None else []
        committed = _state(status, run=None)
        research = research or RecordingResearch(
            order,
            committed=committed,
            event=event or _event(transition, payload_status="running"),
        )
        feed = RecordingFeed(order, note=feed_note, error=feed_error)
        use_case = _use_case(
            research=research,
            artifacts=RecordingArtifacts(order),
            feed=feed,
            tracking=tracking,
            exhibits=RecordingExhibits(order),
        )
        result = use_case.execute(
            experiment_id=EXPERIMENT_ID,
            transition=transition,
            project_id=PROJECT_ID,
        )
        return result, feed, research, order

    def test_feed_event_is_mapped_from_final_state_not_event_payload_or_command(self) -> None:
        cases = (
            ("complete", "mark_failed", "experiment_complete"),
            ("failed", "complete", "experiment_failed"),
            ("abandoned", "complete", "experiment_abandoned"),
        )
        for status, transition, expected_event in cases:
            with self.subTest(status=status, transition=transition):
                event = _event(transition, payload_status="running")
                result, feed, _research, _order = self._execute(
                    status=status, transition=transition, event=event
                )
                self.assertEqual(feed.calls[0]["event"], expected_event)
                self.assertEqual(result["feed_note"], "feed note")

    def test_nonterminal_state_does_not_query_feed(self) -> None:
        result, feed, _research, _order = self._execute(
            status="experiment_review", transition="submit_results"
        )
        self.assertEqual(feed.calls, [])
        self.assertNotIn("feed_note", result)

    def test_feed_failure_is_suppressed(self) -> None:
        result, feed, research, _order = self._execute(
            status="complete",
            transition="complete",
            feed_error=RuntimeError("feed unavailable"),
        )
        self.assertTrue(research.transition_committed)
        self.assertEqual(len(feed.calls), 1)
        self.assertEqual(result["status"], "complete")
        self.assertNotIn("feed_note", result)

    def test_feed_query_is_after_response_context_assembly_and_none_stays_absent(self) -> None:
        order: list[str] = []
        tracking = RecordingTracking(order)
        result, feed, _research, observed = self._execute(
            status="complete",
            transition="complete",
            feed_note=None,
            tracking=tracking,
        )

        self.assertEqual(len(feed.calls), 1)
        self.assertNotIn("feed_note", result)
        self.assertLess(
            observed.index("tracking.context.serialize"),
            observed.index("feed.advisory"),
        )

    def test_context_assembly_failure_propagates_and_skips_feed(self) -> None:
        order: list[str] = []
        tracking = RecordingTracking(
            order, context_error=RuntimeError("context serialization failed")
        )
        committed = _state("complete", run=None)
        research = RecordingResearch(
            order, committed=committed, event=_event("complete")
        )
        feed = RecordingFeed(order)
        use_case = _use_case(
            research=research,
            artifacts=RecordingArtifacts(order),
            feed=feed,
            tracking=tracking,
            exhibits=RecordingExhibits(order),
        )

        with self.assertRaisesRegex(RuntimeError, "context serialization failed"):
            use_case.execute(
                experiment_id=EXPERIMENT_ID,
                transition="complete",
                project_id=PROJECT_ID,
            )

        self.assertTrue(research.transition_committed)
        self.assertEqual(feed.calls, [])
        self.assertNotIn("feed.advisory", order)


class SubmitResultsExhibitPrerequisiteTest(unittest.TestCase):
    def _fixture(
        self,
        *,
        pin_error: Exception | None = None,
        transition_error: Exception | None = None,
        before_status: str = "running",
        runs_found: int = 1,
    ) -> tuple[
        TransitionExperiment,
        RecordingResearch,
        RecordingArtifacts,
        RecordingTracking,
        RecordingFeed,
        RecordingExhibits,
        list[str],
    ]:
        order: list[str] = []
        run = _open_run()
        research = RecordingResearch(
            order,
            before=_state(before_status, run=run, token="before"),
            committed=_state("experiment_review", run=run),
            event=_event("submit_results"),
            transition_error=transition_error,
        )
        artifacts = RecordingArtifacts(order, pin_error=pin_error)
        tracking = RecordingTracking(
            order,
            finalize_result={"run": {**run, "status": "FINISHED"}},
        )
        feed = RecordingFeed(order)
        exhibits = RecordingExhibits(order, exhibit=_exhibit(runs_found=runs_found))
        return (
            _use_case(
                research=research,
                artifacts=artifacts,
                feed=feed,
                tracking=tracking,
                exhibits=exhibits,
            ),
            research,
            artifacts,
            tracking,
            feed,
            exhibits,
            order,
        )

    def test_verdict_and_pin_commit_before_transition_and_pinned_summary_is_returned(self) -> None:
        use_case, research, artifacts, _tracking, _feed, exhibits, order = (
            self._fixture()
        )

        result = use_case.execute(
            experiment_id=EXPERIMENT_ID,
            transition="submit_results",
            project_id=PROJECT_ID,
        )

        self.assertIs(exhibits.states[0], research.before)
        self.assertEqual(
            [
                item
                for item in order
                if item
                in {
                    "research.state",
                    "exhibits.generate",
                    "research.exhibit_verdict",
                    "artifacts.pin",
                    "research.transition",
                }
            ],
            [
                "research.state",
                "exhibits.generate",
                "research.exhibit_verdict",
                "artifacts.pin",
                "research.transition",
            ],
        )
        self.assertEqual(research.verdicts[0]["runs_found"], 1)
        self.assertTrue(research.verdicts[0]["pinned"])
        self.assertEqual(len(artifacts.pins), 1)
        pin = artifacts.pins[0]
        self.assertEqual(pin["experiment_id"], EXPERIMENT_ID)
        self.assertEqual(pin["role"], "exhibit")
        self.assertEqual(pin["content_type"], "application/json")
        self.assertEqual(json.loads(pin["content_bytes"]), _exhibit())
        self.assertEqual(
            result["metrics_exhibit"],
            {
                "pinned": True,
                "path": "experiments/a-characterized-experiment/metrics_exhibit.json",
                "verdict": {"runs_found": 1, "result_files": 0},
            },
        )

    def test_pin_failure_leaves_verdict_but_does_not_transition(self) -> None:
        failure = RuntimeError("pin failed")
        use_case, research, artifacts, tracking, feed, _exhibits, _order = (
            self._fixture(pin_error=failure)
        )

        with self.assertRaisesRegex(RuntimeError, "pin failed"):
            use_case.execute(
                experiment_id=EXPERIMENT_ID,
                transition="submit_results",
                project_id=PROJECT_ID,
            )

        self.assertEqual(len(research.verdicts), 1)
        self.assertTrue(research.verdicts[0]["pinned"])
        self.assertEqual(len(artifacts.pin_attempts), 1)
        self.assertEqual(artifacts.pins, [])
        self.assertEqual(research.transition_calls, [])
        self.assertFalse(research.transition_committed)
        self.assertEqual(tracking.finalize_calls, [])
        self.assertEqual(feed.calls, [])

    def test_gate_failure_after_pin_preserves_prerequisite_residue(self) -> None:
        gate_error = RuntimeError("workflow gate rejected report")
        use_case, research, artifacts, tracking, feed, _exhibits, order = (
            self._fixture(transition_error=gate_error)
        )

        with self.assertRaisesRegex(RuntimeError, "workflow gate rejected report"):
            use_case.execute(
                experiment_id=EXPERIMENT_ID,
                transition="submit_results",
                project_id=PROJECT_ID,
            )

        self.assertEqual(len(research.verdicts), 1)
        self.assertEqual(len(artifacts.pins), 1)
        self.assertEqual(len(research.transition_calls), 1)
        self.assertFalse(research.transition_committed)
        self.assertLess(order.index("artifacts.pin"), order.index("research.transition"))
        self.assertEqual(tracking.finalize_calls, [])
        self.assertEqual(feed.calls, [])

    def test_submit_from_non_running_state_skips_exhibit_prerequisite(self) -> None:
        use_case, research, artifacts, _tracking, _feed, exhibits, order = (
            self._fixture(before_status="experiment_review")
        )

        use_case.execute(
            experiment_id=EXPERIMENT_ID,
            transition="submit_results",
            project_id=PROJECT_ID,
        )

        self.assertEqual(exhibits.states, [])
        self.assertEqual(research.verdicts, [])
        self.assertEqual(artifacts.pin_attempts, [])
        self.assertEqual(
            order[:3],
            ["research.state", "objects.by_experiment", "research.transition"],
        )


class TrackingCredentialFlagTest(unittest.TestCase):
    def test_execute_passes_the_explicit_credential_flag_to_context(self) -> None:
        for include_credentials in (False, True):
            with self.subTest(include_credentials=include_credentials):
                order: list[str] = []
                committed = _state("running", run=_open_run())
                research = RecordingResearch(
                    order,
                    committed=committed,
                    event=_event("start_running"),
                )
                tracking = RecordingTracking(order)
                use_case = _use_case(
                    research=research,
                    artifacts=RecordingArtifacts(order),
                    feed=RecordingFeed(order),
                    tracking=tracking,
                    exhibits=RecordingExhibits(order),
                )

                result = use_case.execute(
                    experiment_id=EXPERIMENT_ID,
                    transition="start_running",
                    project_id=PROJECT_ID,
                    include_tracking_credentials=include_credentials,
                )

                self.assertEqual(
                    tracking.context_calls[0]["include_credentials"],
                    include_credentials,
                )
                password = result["mlflow"]["env"].get(
                    "MLFLOW_TRACKING_PASSWORD"
                )
                self.assertEqual(
                    password,
                    "credential-for-public-response" if include_credentials else None,
                )


if __name__ == "__main__":
    unittest.main()
