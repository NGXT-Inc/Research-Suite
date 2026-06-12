"""Declarative workflow gate table — the single source of truth for gates.

Each non-terminal experiment status has exactly one *forward* transition. Its
``ForwardTransition`` entry declares everything every consumer needs, so the
three surfaces that used to hand-maintain parallel copies cannot drift:

- ENFORCEMENT — ``ExperimentService._next_status`` walks ``requirements`` (and
  ``review``), raising ``WorkflowError`` with the entry's ``error`` text on the
  first unmet one.
- GUIDANCE — ``WorkflowService._workflow_for`` walks the same ``requirements``;
  the first role missing from the current attempt yields that requirement's
  gate/action/allowed payload, and once all are present the transition's
  ``ready_*`` fields say "go transition". Review-gated statuses get their
  guidance from ``review`` (role, skill, action stem).
- DISCOVERY — ``experiment.get_state.allowed_transitions`` surfaces
  ``requires_prose`` so the agent learns preconditions before trial-and-error.

``abandon``/``mark_failed`` stay out of the table (available from any
non-terminal status), and terminal statuses have no forward transition.

SYSTEM TRANSITIONS are sandbox-lifecycle moves (``sandbox_started``,
``sandbox_expired``). They live in ``TRANSITION_GRAPH`` so the workflow engine
is the single writer of experiment status, but they are *not* agent-callable:
``allowed_transitions_for`` hides them and ``experiment.transition`` rejects
them. The sandbox registry applies them via
``ExperimentService.apply_system_transition``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TERMINAL_STATUSES = frozenset({"complete", "failed", "abandoned"})
ACTIVE_PROCESS_STATUSES = frozenset({"provisioning", "running"})


@dataclass(frozen=True)
class RoleRequirement:
    """A current-attempt resource association the forward transition needs."""

    role: str
    # Enforcement: WorkflowError message when the association is absent.
    error: str
    # Enforcement: deep-lint hook run after the association exists
    # ("plan" | "report" | ""). The lint reads the live file.
    validator: str = ""
    # Guidance while unmet: current_gate / next_action / allowed_actions /
    # missing_evidence entry / resource_guidance payload key.
    gate: str = ""
    action: str = ""
    allowed: tuple[str, ...] = ()
    missing: str = ""
    guidance_key: str = ""


@dataclass(frozen=True)
class ReviewRequirement:
    """A passing review the forward transition needs."""

    role: str  # design_reviewer | experiment_reviewer
    skill: str  # reviewer skill the orienting agent launches
    action_name: str  # stem for launch_{...}er / wait_for_{...} / {...}_passed
    error: str  # enforcement message when no passing review exists
    pass_action: str  # guidance next_action once the verdict is pass


@dataclass(frozen=True)
class ForwardTransition:
    """The one forward transition out of a status, with its gate contract."""

    name: str
    to_status: str
    requires_prose: str = ""
    requirements: tuple[RoleRequirement, ...] = ()
    review: ReviewRequirement | None = None
    # Guidance once every requirement is met: "go transition".
    ready_gate: str = ""
    ready_action: str = ""
    ready_allowed: tuple[str, ...] = ()


GATE_TABLE: dict[str, ForwardTransition] = {
    "planned": ForwardTransition(
        name="submit_design",
        to_status="design_review",
        requires_prose=(
            "a 'plan' resource must be synced & associated to this experiment, with "
            "the required plan section headers present"
        ),
        requirements=(
            RoleRequirement(
                role="plan",
                error="an experiment plan resource must be synced before design review",
                validator="plan",
                gate="plan_required",
                action="write_or_sync_plan_resource",
                allowed=("resource.register_file", "resource.associate"),
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
            skill="design-review",
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
        ready_allowed=("sandbox.request", "experiment.transition"),
    ),
    "running": ForwardTransition(
        name="submit_results",
        to_status="experiment_review",
        requires_prose=(
            "a 'result' resource, a results report (role 'report'), AND a logic "
            "graph (role 'graph') must be synced & associated to this experiment; "
            "the report needs the required section headers, a metrics table, and "
            "resolvable figure links; the graph must be valid JSON forming a DAG "
            "of at most 16 nodes"
        ),
        requirements=(
            RoleRequirement(
                role="result",
                error="result resource must be synced before experiment_review",
                # The workflow layer upgrades this to execution_active while a
                # sandbox is live for the experiment.
                gate="execution_ready",
                action="run_experiment_and_sync_results",
                allowed=(
                    "sandbox.request",
                    "sandbox.terminal",
                    "sandbox.get",
                    "sandbox.sync",
                    "resource.register_file",
                    "resource.associate",
                ),
                missing="result resource",
                guidance_key="result",
            ),
            RoleRequirement(
                role="report",
                error=(
                    "a results report must be synced before experiment_review: write a "
                    "short markdown report (sections Summary; Results with a metrics "
                    "table; Deviations from plan; Conclusion applying the plan's "
                    "decision rule), sync it, and associate it with role 'report' — "
                    "see skills/research-workflow/report-template.md"
                ),
                validator="report",
                gate="results_report_required",
                action="write_and_associate_results_report",
                allowed=(
                    "sandbox.sync",
                    "resource.register_file",
                    "resource.associate",
                ),
                missing="results report resource (role 'report')",
                guidance_key="report",
            ),
            RoleRequirement(
                role="graph",
                error=(
                    "a logic graph must be synced before experiment_review: write "
                    "the experiment's logic graph (experiments/<name>/graph.json "
                    "— your story of the experiment's logical path: the hard "
                    "decisions and the reasoning behind them, as a DAG of at most "
                    "16 nodes; not a pipeline/provenance diagram and never "
                    "script-generated), sync it, and associate it "
                    "with role 'graph' — see skills/research-workflow/graph-template.md"
                ),
                validator="graph",
                gate="logic_graph_required",
                action="write_and_associate_logic_graph",
                allowed=(
                    "sandbox.sync",
                    "resource.register_file",
                    "resource.associate",
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
            "is present, the last review rejected this attempt — address "
            "it before resubmitting)"
        ),
        ready_allowed=("experiment.transition",),
    ),
    "experiment_review": ForwardTransition(
        name="complete",
        to_status="complete",
        requires_prose="a passing experiment_reviewer review",
        review=ReviewRequirement(
            role="experiment_reviewer",
            skill="experiment-review",
            action_name="experiment_review",
            error="experiment review must pass before complete",
            pass_action="complete_experiment",
        ),
    ),
}


# Sandbox-lifecycle transitions. In the graph (the workflow engine is the only
# writer of experiment status) but never agent-callable.
SYSTEM_TRANSITIONS = frozenset({"sandbox_started", "sandbox_expired"})

# (from_status, transition) -> next_status. Derived from GATE_TABLE plus the
# system transitions, so the graph and the gate contracts cannot diverge.
TRANSITION_GRAPH: dict[tuple[str, str], str] = {
    (status, forward.name): forward.to_status
    for status, forward in GATE_TABLE.items()
}
TRANSITION_GRAPH.update(
    {
        ("ready_to_run", "sandbox_started"): "running",
        ("running", "sandbox_expired"): "ready_to_run",
    }
)

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
    trial-and-error. System (sandbox-lifecycle) transitions are excluded: the
    agent cannot call them.
    """
    if status in TERMINAL_STATUSES:
        return []
    out: list[dict[str, Any]] = []
    for (frm, transition), nxt in TRANSITION_GRAPH.items():
        if frm == status and transition not in SYSTEM_TRANSITIONS:
            entry: dict[str, Any] = {"transition": transition, "leads_to": nxt}
            if transition in TRANSITION_REQUIREMENTS:
                entry["requires"] = TRANSITION_REQUIREMENTS[transition]
            out.append(entry)
    out.append({"transition": "abandon", "leads_to": "abandoned"})
    out.append({"transition": "mark_failed", "leads_to": "failed"})
    return out
