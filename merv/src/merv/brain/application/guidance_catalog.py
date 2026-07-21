"""Application-owned catalog of agent actions, tools, and reviewer skills."""

from __future__ import annotations

from typing import NamedTuple


class RequirementGuidance(NamedTuple):
    action: str
    allowed: tuple[str, ...]
    resource_key: str


class ReadyGuidance(NamedTuple):
    gate: str
    action: str
    allowed: tuple[str, ...]


class ReviewGuidance(NamedTuple):
    skill: str
    action_name: str
    pass_action: str


EXPERIMENT_REQUIREMENTS = {
    "plan": RequirementGuidance(
        "write_and_associate_plan_resource", ("resource.register",), "plan"
    ),
    "result": RequirementGuidance(
        "run_experiment_and_retain_results",
        (
            "sandbox.request",
            "sandbox.attach",
            "sandbox.terminal",
            "sandbox.get",
            "experiment.transition",
            "resource.register",
        ),
        "result",
    ),
    "report": RequirementGuidance(
        "write_and_associate_results_report", ("resource.register",), "report"
    ),
    "graph": RequirementGuidance(
        "write_and_associate_logic_graph", ("resource.register",), "graph"
    ),
}

EXPERIMENT_READY = {
    "submit_design": ReadyGuidance(
        "design_review_required", "submit_design_for_review", ("experiment.transition",)
    ),
    "start_running": ReadyGuidance(
        "execution_ready",
        "start_running",
        ("sandbox.request", "sandbox.attach", "experiment.transition"),
    ),
    "submit_results": ReadyGuidance(
        "experiment_review_required",
        (
            "submit_results_for_review (call only once the experiment is fully "
            "complete and every success criterion in the experiment intent is "
            "satisfied; do NOT call if the experiment should continue running; "
            "continue with sandbox.* and resource.* calls instead and only "
            "transition once the work is truly done; if revision_context is "
            "present, the last review rejected this attempt or an infrastructure "
            "retry was requested — address it before resubmitting)"
        ),
        ("experiment.transition",),
    ),
}

REFLECTION_REQUIREMENTS = {
    "reflection_lens_doc": RequirementGuidance(
        "fan_out_reflection_subagents", ("resource.register",), "reflection"
    ),
    "project_graph": RequirementGuidance(
        "update_and_associate_project_graph", ("resource.register",), "project_graph"
    ),
    "reflection_doc": RequirementGuidance(
        "write_and_associate_reflection_doc", ("resource.register",), "reflection_doc"
    ),
    "change_spec": RequirementGuidance(
        "write_and_associate_change_spec", ("resource.register",), "change_spec"
    ),
}

REFLECTION_READY = {
    "submit_reflections": ReadyGuidance(
        "reflections_complete", "submit_reflections", ("reflection.transition",)
    ),
    "submit_reflection_artifacts": ReadyGuidance(
        "reflection_review_required",
        (
            "submit_reflection_artifacts (call only once the project graph "
            "reflects the reconciled reasoning state, the reflection doc explains "
            "the scientific argument concisely, and the change spec represents "
            "the intended belief-state update; if revision_context is present, "
            "the last review rejected this attempt — address it before "
            "resubmitting)"
        ),
        ("reflection.transition",),
    ),
}

REVIEWS = {
    "design_reviewer": ReviewGuidance(
        "experiment-design-review", "design_review", "mark_ready_to_run"
    ),
    "experiment_reviewer": ReviewGuidance(
        "experiment-attempt-review", "experiment_review", "complete_experiment"
    ),
    "reflection_reviewer": ReviewGuidance(
        "project-reflection-review", "reflection_review", "publish_reflection"
    ),
}


__all__ = [
    "EXPERIMENT_READY",
    "EXPERIMENT_REQUIREMENTS",
    "REFLECTION_READY",
    "REFLECTION_REQUIREMENTS",
    "REVIEWS",
    "ReadyGuidance",
    "RequirementGuidance",
    "ReviewGuidance",
]
