"""Declarative workflow gate table — the single source of truth for gates.

Each non-terminal experiment status has exactly one *forward* transition. Its
``ForwardTransition`` entry declares everything every consumer needs, so the
three surfaces that used to hand-maintain parallel copies cannot drift:

- ENFORCEMENT — ``ExperimentService._next_status`` walks ``requirements`` (and
  ``review``), raising ``WorkflowError`` with the entry's ``error`` text on the
  first unmet one.
- GUIDANCE — ``NextActionPolicy.experiment`` walks the same ``requirements``;
  the first role missing from the current attempt yields that requirement's
  gate/action/allowed payload, and once all are present the transition's
  ``ready_*`` fields say "go transition". Review-gated statuses get their
  guidance from ``review`` (role, skill, action stem).
- DISCOVERY — ``experiment.get_state.allowed_transitions`` surfaces
  ``requires_prose`` so the agent learns preconditions before trial-and-error.

``abandon``/``mark_failed`` stay out of the table (available from any
non-terminal status), and terminal statuses have no forward transition.
"""

from __future__ import annotations

from typing import Any

from .gates import ForwardTransition, ReviewRequirement, RoleRequirement
from .vocabulary import (
    EXPERIMENT_ACTIVE_PROCESS_STATUSES,
    EXPERIMENT_TERMINAL_STATUSES,
)

TERMINAL_STATUSES = EXPERIMENT_TERMINAL_STATUSES
ACTIVE_PROCESS_STATUSES = EXPERIMENT_ACTIVE_PROCESS_STATUSES


GATE_TABLE: dict[str, ForwardTransition] = {
    "planned": ForwardTransition(
        name="submit_design",
        to_status="design_review",
        requires_prose=(
            "a 'plan' resource must be registered and associated to this experiment, with "
            "the required plan section headers present"
        ),
        requirements=(
            RoleRequirement(
                role="plan",
                error="an experiment plan resource must be registered before design review",
                validator="plan",
                gate="plan_required",
                action="write_and_associate_plan_resource",
                allowed=("resource.register",),
                missing="experiment plan resource",
                guidance_key="plan",
            ),
        ),
        ready_gate="design_review_required",
        ready_action="submit_design_for_review",
        ready_allowed=("experiment.transition",),
    ),
    "design_review": ForwardTransition(
        name="mark_ready_to_run",
        to_status="ready_to_run",
        requires_prose="a passing design_reviewer review",
        review=ReviewRequirement(
            role="design_reviewer",
            skill="experiment-design-review",
            action_name="design_review",
            error="design review must pass before ready_to_run",
            pass_action="mark_ready_to_run",
        ),
    ),
    "ready_to_run": ForwardTransition(
        name="start_running",
        to_status="running",
        ready_gate="execution_ready",
        ready_action="start_running",
        ready_allowed=("sandbox.request", "sandbox.attach", "experiment.transition"),
    ),
    "running": ForwardTransition(
        name="submit_results",
        to_status="experiment_review",
        requires_prose=(
            "a 'result' resource, a results report (role 'report'), AND a logic "
            "graph (role 'graph') must be registered and associated to this experiment; "
            "the report needs the required section headers, resolvable figure links, "
            "and — when the system pinned a metrics exhibit for this attempt — a "
            "reference to it (the exhibit, not an agent-written table, is the "
            "record of the attempt's runs); the graph must be valid JSON forming "
            "a DAG of at most 16 nodes"
        ),
        requirements=(
            RoleRequirement(
                role="result",
                error="result resource must be retained before experiment_review",
                # The workflow layer upgrades this to execution_active while a
                # sandbox is live for the experiment.
                gate="execution_ready",
                action="run_experiment_and_retain_results",
                allowed=(
                    "sandbox.request",
                    "sandbox.attach",
                    "sandbox.terminal",
                    "sandbox.get",
                    "experiment.transition",
                    "resource.register",
                ),
                missing="result resource",
                guidance_key="result",
            ),
            RoleRequirement(
                role="report",
                error=(
                    "a results report must be retained before experiment_review: write a "
                    "short markdown report (sections Summary; Results interpreting the "
                    "system metrics exhibit — preview it with experiment.exhibit; "
                    "Deviations from plan; Conclusion applying the plan's "
                    "decision rule), copy it out if produced on the sandbox, and "
                    "associate it with role 'report' — "
                    "see skills/research-workflow/report-template.md"
                ),
                validator="report",
                gate="results_report_required",
                action="write_and_associate_results_report",
                allowed=(
                    "resource.register",
                ),
                missing="results report resource (role 'report')",
                guidance_key="report",
            ),
            RoleRequirement(
                role="graph",
                error=(
                    "a logic graph must be retained before experiment_review: write "
                    "the experiment's logic graph (experiments/<name>/graph.json "
                    "— your story of the experiment's logical path: the hard "
                    "decisions and the reasoning behind them, as a DAG of at most "
                    "16 nodes; not a pipeline/provenance diagram and never "
                    "script-generated), copy it out if produced on the sandbox, and associate it "
                    "with role 'graph' — see skills/research-workflow/graph-template.md"
                ),
                validator="graph",
                gate="logic_graph_required",
                action="write_and_associate_logic_graph",
                allowed=(
                    "resource.register",
                ),
                missing="logic graph resource (role 'graph')",
                guidance_key="graph",
            ),
        ),
        ready_gate="experiment_review_required",
        ready_action=(
            "submit_results_for_review (call only once the experiment "
            "is fully complete and every success criterion in the "
            "experiment intent is satisfied; do NOT call if the "
            "experiment should continue running; continue with "
            "sandbox.* and resource.* calls instead and only "
            "transition once the work is truly done; if revision_context "
            "is present, the last review rejected this attempt or an "
            "infrastructure retry was requested — address it before "
            "resubmitting)"
        ),
        ready_allowed=("experiment.transition",),
    ),
    "experiment_review": ForwardTransition(
        name="complete",
        to_status="complete",
        requires_prose="a passing experiment_reviewer review",
        review=ReviewRequirement(
            role="experiment_reviewer",
            skill="experiment-attempt-review",
            action_name="experiment_review",
            error="experiment review must pass before complete",
            pass_action="complete_experiment",
        ),
    ),
}


SYSTEM_TRANSITIONS = frozenset()

# (from_status, transition) -> next_status. Derived from GATE_TABLE so the graph
# and the gate contracts cannot diverge.
TRANSITION_GRAPH: dict[tuple[str, str], str] = {
    (status, forward.name): forward.to_status
    for status, forward in GATE_TABLE.items()
}

# Plain-language preconditions surfaced on experiment.get_state and in
# 'not allowed' errors. Derived from the same table.
TRANSITION_REQUIREMENTS: dict[str, str] = {
    forward.name: forward.requires_prose
    for forward in GATE_TABLE.values()
    if forward.requires_prose
}


def allowed_transitions_for(status: str) -> list[dict[str, Any]]:
    """Agent-callable transitions available from ``status``, with hints.

    Surfaced on ``experiment.get_state`` and in 'not allowed' errors so the
    agent can see what to do next (and what each step requires) without
    trial-and-error.
    """
    if status in TERMINAL_STATUSES:
        return []
    out: list[dict[str, Any]] = []
    for (frm, transition), nxt in TRANSITION_GRAPH.items():
        if frm == status:
            entry: dict[str, Any] = {"transition": transition, "leads_to": nxt}
            if transition in TRANSITION_REQUIREMENTS:
                entry["requires"] = TRANSITION_REQUIREMENTS[transition]
            out.append(entry)
    if status == "running":
        out.append(
            {
                "transition": "retry_running",
                "leads_to": "running",
                "requires": (
                    "use only for infrastructure failure or interrupted "
                    "execution when the approved plan still stands; the "
                    "experiment remains running and attempt_index is unchanged"
                ),
            }
        )
    out.append({"transition": "abandon", "leads_to": "abandoned"})
    out.append({"transition": "mark_failed", "leads_to": "failed"})
    return out
