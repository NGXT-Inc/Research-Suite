"""Declarative workflow gate table — the single source of truth for gates.

Each non-terminal experiment status has exactly one *forward* transition. Its
``ForwardTransition`` entry declares everything every consumer needs, so the
three surfaces that used to hand-maintain parallel copies cannot drift:

- ENFORCEMENT — ``ExperimentService._evaluate_gate`` evaluates the contract and
  ``GateEvaluation.require_transition`` raises ``WorkflowError`` with the
  entry's ``error`` text on the first unmet requirement.
- GUIDANCE — Research exposes the evaluated requirements through its facade;
  Application maps those authoritative facts to agent-facing advice.
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
                missing="experiment plan resource",
                label="Plan associated and valid",
            ),
        ),
    ),
    "design_review": ForwardTransition(
        name="mark_ready_to_run",
        to_status="ready_to_run",
        requires_prose="a passing design_reviewer review",
        review=ReviewRequirement(
            role="design_reviewer",
            error="design review must pass before ready_to_run",
            blocker_code="design_review_required",
            label="Design review passed",
        ),
    ),
    "ready_to_run": ForwardTransition(
        name="start_running",
        to_status="running",
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
                missing="result resource",
                label="Result resource present",
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
                missing="results report resource (role 'report')",
                label="Results report present and valid",
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
                missing="logic graph resource (role 'graph')",
                label="Logic graph present and valid",
            ),
        ),
    ),
    "experiment_review": ForwardTransition(
        name="complete",
        to_status="complete",
        requires_prose="a passing experiment_reviewer review",
        review=ReviewRequirement(
            role="experiment_reviewer",
            error="experiment review must pass before complete",
            blocker_code="experiment_review_required",
            label="Experiment review passed",
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
