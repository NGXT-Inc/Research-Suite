"""SURFACE-layer wiring for the event-carried feed_note advisory.

feed.py owns the posts table and the dedupe decision (feed_note_for); this
module owns *attaching* that note to the responses of tools an agent already
calls for other reasons — experiment.transition (into a terminal state),
mlflow.finalize_run, and review.status. These are lightweight stub-based unit
tests of that wiring in src/merv/brain/surface/tools/tool_handlers.py: they build the
handler dict directly (the same helper tests/surface/test_tool_contracts.py
uses) with tiny recording stubs, so the terminal-status/verdict/finalize
branching and the "never raise" contract can be checked without a full
gate/review/MLflow stack. The real end-to-end path (a genuine experiment
driven to `complete` through the actual services) is covered separately in
tests/workflow/test_feed.py.
"""

from __future__ import annotations

import unittest
from typing import Any

from merv.brain.surface.tools.tool_handlers import build_control_tool_handlers


class _Unused:
    """Stands in for any service this test doesn't exercise: attribute
    access yields a callable that returns {} (see _HandlerTarget in
    test_tool_contracts.py — same idea, local copy to keep this file
    self-contained)."""

    def __getattr__(self, _name: str):
        def _handler(**_kwargs: Any) -> dict[str, Any]:
            return {}

        return _handler


class _StubFeed:
    """Records every feed_note_for call; returns a canned note unless the
    entity is muted, or raises if configured to (to exercise the "a feed
    hiccup must never block the workflow" rule)."""

    def __init__(self, *, muted: set[str] | None = None, raises: bool = False) -> None:
        self.muted = muted or set()
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, _name: str) -> Any:
        def _handler(**_kwargs: Any) -> dict[str, Any]:
            return {}

        return _handler

    def feed_note_for(self, *, project_id: str, entity_id: str, event: str) -> str | None:
        self.calls.append(
            {"project_id": project_id, "entity_id": entity_id, "event": event}
        )
        if self.raises:
            raise RuntimeError("feed hiccup")
        if entity_id in self.muted:
            return None
        return f"{entity_id} just had an update ({event})."


def _fallback_handler(**_kwargs: Any) -> dict[str, Any]:
    """Shared no-op for any method a stub below doesn't explicitly implement
    but that build_control_tool_handlers references eagerly (bare
    passthroughs like ``experiments.create`` in its handlers dict)."""
    return {}


class _StubExperiments:
    """get_state/transition/record_mlflow_run all just hand back the fixed
    state this test configured — enough shape for slim_experiment_state and
    the terminal-status branch in experiment_transition_agent."""

    def __init__(self, *, state: dict[str, Any]) -> None:
        self.state = state

    def __getattr__(self, _name: str) -> Any:
        return _fallback_handler

    def get_state(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        return dict(self.state)

    def transition(
        self,
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return dict(self.state)

    def record_mlflow_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run: dict[str, Any],
        event_type: str | None = None,
    ) -> dict[str, Any]:
        merged = dict(self.state)
        merged["mlflow_run"] = run
        return merged


class _BoomExperiments:
    """get_state always raises — simulates project_id resolution failing."""

    def __getattr__(self, _name: str) -> Any:
        return _fallback_handler

    def get_state(
        self, *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        raise RuntimeError("boom")


class _StubMlflowTracking:
    def __init__(
        self, *, finalize_result: dict[str, Any], raises: bool = False
    ) -> None:
        self._finalize_result = finalize_result
        self.raises = raises
        self.finalize_calls: list[dict[str, Any]] = []

    def context(
        self, *, project_id: str, experiment_id: str, include_credentials: bool = False
    ) -> Any:
        class _Ctx:
            def to_dict(self_inner) -> dict[str, Any]:
                return {"configured": True}

        return _Ctx()

    def finalize_run(
        self,
        *,
        project_id: str,
        experiment_id: str,
        run_id: str,
        status: str | None = "FINISHED",
        wait_seconds: float = 2.0,
    ) -> dict[str, Any]:
        self.finalize_calls.append({
            "project_id": project_id,
            "experiment_id": experiment_id,
            "run_id": run_id,
            "status": status,
            "wait_seconds": wait_seconds,
        })
        if self.raises:
            raise RuntimeError("mlflow down")
        return dict(self._finalize_result)


class _StubReviews:
    def __init__(self, *, result: dict[str, Any]) -> None:
        self._result = result

    def __getattr__(self, _name: str) -> Any:
        return _fallback_handler

    def status(
        self, *, target_type: str, target_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        return dict(self._result)


def _target(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow": _Unused(),
        "projects": _Unused(),
        "project_overview": _Unused(),
        "claims": _Unused(),
        "experiments": _Unused(),
        "reflection_tools": _Unused(),
        "resources": _Unused(),
        "storage": None,
        "reviews": _Unused(),
        "sandboxes": _Unused(),
        "mlflow_tracking": _Unused(),
        "feed": _Unused(),
    }
    base.update(overrides)
    return base


class ExperimentTransitionFeedNoteTest(unittest.TestCase):
    def test_attaches_note_on_transition_into_complete(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_1", "project_id": "proj_1", "status": "complete"}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(experiments=experiments, feed=feed, mlflow_tracking=None)
        )
        result = handlers["experiment.transition"](
            experiment_id="exp_1", transition="complete", project_id="proj_1"
        )
        self.assertEqual(result["feed_note"], "exp_1 just had an update (experiment_complete).")
        self.assertEqual(
            feed.calls[-1],
            {"project_id": "proj_1", "entity_id": "exp_1", "event": "experiment_complete"},
        )

    def test_attaches_note_on_transition_into_abandoned(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_2", "project_id": "proj_1", "status": "abandoned"}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(experiments=experiments, feed=feed, mlflow_tracking=None)
        )
        result = handlers["experiment.transition"](
            experiment_id="exp_2", transition="abandon", project_id="proj_1"
        )
        self.assertIn("feed_note", result)
        self.assertEqual(feed.calls[-1]["event"], "experiment_abandoned")

    def test_attaches_note_on_transition_into_failed(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_3", "project_id": "proj_1", "status": "failed"}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(experiments=experiments, feed=feed, mlflow_tracking=None)
        )
        result = handlers["experiment.transition"](
            experiment_id="exp_3", transition="mark_failed", project_id="proj_1"
        )
        self.assertIn("feed_note", result)
        self.assertEqual(feed.calls[-1]["event"], "experiment_failed")

    def test_no_note_on_a_non_terminal_transition(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_4", "project_id": "proj_1", "status": "running"}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(experiments=experiments, feed=feed, mlflow_tracking=None)
        )
        result = handlers["experiment.transition"](
            experiment_id="exp_4", transition="start_running", project_id="proj_1"
        )
        self.assertNotIn("feed_note", result)
        self.assertEqual(feed.calls, [])

    def test_feed_hiccup_never_breaks_the_transition_response(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_5", "project_id": "proj_1", "status": "complete"}
        )
        feed = _StubFeed(raises=True)
        handlers = build_control_tool_handlers(
            **_target(experiments=experiments, feed=feed, mlflow_tracking=None)
        )
        result = handlers["experiment.transition"](
            experiment_id="exp_5", transition="complete", project_id="proj_1"
        )
        self.assertEqual(result["status"], "complete")
        self.assertNotIn("feed_note", result)


class ExperimentTransitionMlflowFinalizeTest(unittest.TestCase):
    def test_terminal_transitions_finalize_the_plugin_run_with_mapped_status(self) -> None:
        cases = (
            ("submit_results", "experiment_review", "FINISHED"),
            ("complete", "complete", "FINISHED"),
            ("abandon", "abandoned", "KILLED"),
            ("mark_failed", "failed", "FAILED"),
        )
        for transition, experiment_status, mlflow_status in cases:
            with self.subTest(transition=transition):
                experiments = _StubExperiments(state={
                    "id": "exp_1",
                    "project_id": "proj_1",
                    "status": experiment_status,
                    "mlflow_run": {
                        "run_id": "run_1",
                        "status": "RUNNING",
                        "created_by_plugin": True,
                    },
                })
                mlflow = _StubMlflowTracking(finalize_result={
                    "run": {"run_id": "run_1", "status": mlflow_status}
                })
                handlers = build_control_tool_handlers(**_target(
                    experiments=experiments,
                    mlflow_tracking=mlflow,
                    feed=_StubFeed(muted={"exp_1"}),
                ))

                result = handlers["experiment.transition"](
                    experiment_id="exp_1",
                    transition=transition,
                    project_id="proj_1",
                )

                self.assertEqual(mlflow.finalize_calls[0]["status"], mlflow_status)
                self.assertEqual(mlflow.finalize_calls[0]["run_id"], "run_1")
                self.assertEqual(mlflow.finalize_calls[0]["wait_seconds"], 0.0)
                self.assertEqual(result["mlflow_run"]["status"], mlflow_status)

    def test_mlflow_finalize_error_never_blocks_terminal_transition(self) -> None:
        experiments = _StubExperiments(state={
            "id": "exp_1",
            "project_id": "proj_1",
            "status": "failed",
            "mlflow_run": {
                "run_id": "run_1",
                "status": "RUNNING",
                "created_by_plugin": True,
            },
        })
        mlflow = _StubMlflowTracking(finalize_result={}, raises=True)
        handlers = build_control_tool_handlers(**_target(
            experiments=experiments,
            mlflow_tracking=mlflow,
            feed=_StubFeed(muted={"exp_1"}),
        ))

        result = handlers["experiment.transition"](
            experiment_id="exp_1",
            transition="mark_failed",
            project_id="proj_1",
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(mlflow.finalize_calls), 1)

    def test_non_plugin_and_already_terminal_runs_are_not_finalized(self) -> None:
        for created_by_plugin, status in ((False, "RUNNING"), (True, "FAILED")):
            with self.subTest(
                created_by_plugin=created_by_plugin, status=status
            ):
                experiments = _StubExperiments(state={
                    "id": "exp_1",
                    "project_id": "proj_1",
                    "status": "complete",
                    "mlflow_run": {
                        "run_id": "run_1",
                        "status": status,
                        "created_by_plugin": created_by_plugin,
                    },
                })
                mlflow = _StubMlflowTracking(finalize_result={})
                handlers = build_control_tool_handlers(**_target(
                    experiments=experiments,
                    mlflow_tracking=mlflow,
                    feed=_StubFeed(muted={"exp_1"}),
                ))

                result = handlers["experiment.transition"](
                    experiment_id="exp_1",
                    transition="complete",
                    project_id="proj_1",
                )

                self.assertEqual(result["mlflow_run"]["status"], status)
                self.assertEqual(mlflow.finalize_calls, [])


class MlflowFinalizeRunFeedNoteTest(unittest.TestCase):
    def test_attaches_note_when_a_run_is_finalized(self) -> None:
        experiments = _StubExperiments(
            state={
                "id": "exp_6",
                "project_id": "proj_1",
                "status": "experiment_review",
                "mlflow_run": {},
            }
        )
        mlflow_tracking = _StubMlflowTracking(
            finalize_result={"run": {"run_id": "run_1", "status": "FINISHED"}}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(experiments=experiments, feed=feed, mlflow_tracking=mlflow_tracking)
        )
        result = handlers["mlflow.finalize_run"](project_id="proj_1", experiment_id="exp_6")
        self.assertIn("feed_note", result)
        self.assertEqual(feed.calls[-1]["event"], "mlflow_run_finalized")
        self.assertEqual(feed.calls[-1]["entity_id"], "exp_6")

    def test_no_note_when_mlflow_is_not_configured(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_7", "project_id": "proj_1", "status": "running"}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(experiments=experiments, feed=feed, mlflow_tracking=None)
        )
        result = handlers["mlflow.finalize_run"](project_id="proj_1", experiment_id="exp_7")
        self.assertNotIn("feed_note", result)
        self.assertEqual(feed.calls, [])

    def test_no_note_when_finalize_returns_no_run(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_8", "project_id": "proj_1", "status": "running"}
        )
        mlflow_tracking = _StubMlflowTracking(finalize_result={"error": "boom"})
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(experiments=experiments, feed=feed, mlflow_tracking=mlflow_tracking)
        )
        result = handlers["mlflow.finalize_run"](project_id="proj_1", experiment_id="exp_8")
        self.assertNotIn("feed_note", result)
        self.assertEqual(feed.calls, [])


class ReviewStatusFeedNoteTest(unittest.TestCase):
    def test_attaches_note_when_a_verdict_exists_for_an_experiment(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_9", "project_id": "proj_1", "status": "planned"}
        )
        reviews = _StubReviews(
            result={"requests": [], "reviews": [{"id": "rev_1", "verdict": "pass"}]}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(
                experiments=experiments, reviews=reviews, feed=feed, mlflow_tracking=None
            )
        )
        result = handlers["review.status"](
            target_type="experiment", target_id="exp_9", project_id="proj_1"
        )
        self.assertIn("feed_note", result)
        self.assertEqual(
            feed.calls[-1],
            {
                "project_id": "proj_1",
                "entity_id": "exp_9",
                "event": "experiment_review_verdict",
            },
        )

    def test_no_note_while_review_is_only_pending(self) -> None:
        experiments = _StubExperiments(
            state={"id": "exp_10", "project_id": "proj_1", "status": "design_review"}
        )
        reviews = _StubReviews(
            result={"requests": [{"id": "req_1", "status": "requested"}], "reviews": []}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(
                experiments=experiments, reviews=reviews, feed=feed, mlflow_tracking=None
            )
        )
        result = handlers["review.status"](
            target_type="experiment", target_id="exp_10", project_id="proj_1"
        )
        self.assertNotIn("feed_note", result)
        self.assertEqual(feed.calls, [])

    def test_no_note_for_non_experiment_targets(self) -> None:
        reviews = _StubReviews(
            result={"requests": [], "reviews": [{"id": "rev_2", "verdict": "pass"}]}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(reviews=reviews, feed=feed, mlflow_tracking=None)
        )
        result = handlers["review.status"](
            target_type="reflection", target_id="syn_1", project_id="proj_1"
        )
        self.assertNotIn("feed_note", result)
        self.assertEqual(feed.calls, [])

    def test_project_id_resolution_failure_never_breaks_review_status(self) -> None:
        reviews = _StubReviews(
            result={"requests": [], "reviews": [{"id": "rev_3", "verdict": "pass"}]}
        )
        feed = _StubFeed()
        handlers = build_control_tool_handlers(
            **_target(
                experiments=_BoomExperiments(),
                reviews=reviews,
                feed=feed,
                mlflow_tracking=None,
            )
        )
        result = handlers["review.status"](
            target_type="experiment", target_id="exp_11", project_id="proj_1"
        )
        self.assertNotIn("feed_note", result)
        self.assertEqual(result["reviews"][0]["id"], "rev_3")


if __name__ == "__main__":
    unittest.main()
