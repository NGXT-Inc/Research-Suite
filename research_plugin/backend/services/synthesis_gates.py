"""Declarative gate table for the project-synthesis (reflection wave) workflow.

A synthesis record is one project-level reflection wave: a roster of
differentiated reflection lenses fans out, each lens submits its own
reflection, the orchestrator reconciles them into the living project logic
graph plus a what's-next proposals file, and a synthesis reviewer judges the
result against the corpus. The FSM is deliberately smaller than the
experiment one — nothing executes, so there is no sandbox lifecycle:

    reflecting ──submit_reflections──▶ synthesizing ──submit_synthesis──▶
        synthesis_review ──publish──▶ published

Review rejections route back through two distinct targets (mirroring the
experiment planned/running split): ``return_to='reflecting'`` re-launches the
fan-out (attempt bump — every lens must submit a fresh reflection), while
``return_to='synthesizing'`` keeps the reflections standing and only the
synthesis (graph + proposals) is revised.

The same three consumers as ``workflow_gates.GATE_TABLE`` read this table —
enforcement (``SynthesisService._next_status``), guidance
(``WorkflowService._synthesis_workflow_for``), and discovery
(``allowed_synthesis_transitions_for``) — reusing the same dataclasses so the
two workflows cannot drift in shape.

All gates here check envelopes only (files exist, the roster is covered, the
graph parses within budget). Whether the synthesis is honest and the
proposals are real is the reviewer's call, and the diversity heuristics live
in the research-reflection skill, not in gates.
"""

from __future__ import annotations

from typing import Any

from .workflow_gates import ForwardTransition, ReviewRequirement, RoleRequirement


SYNTHESIS_TERMINAL_STATUSES = frozenset({"published", "abandoned"})

# Engineered diversity: every wave fans out exactly five lenses — the three
# core ones below plus two the orchestrator designs for this specific project
# (each with a stated reason it is distinct). The full lens briefs live in
# skills/research-reflection; these charters are the durable one-line versions
# recorded on the roster when the agent does not supply its own wording.
ROSTER_SIZE = 5

CORE_LENSES: tuple[dict[str, str], ...] = (
    {
        "id": "outcomes",
        "title": "Outcomes & evidence",
        "charter": (
            "What do we actually know? Assemble the verified knowledge state "
            "from claims, experiment outcomes, and review verdicts: what is "
            "established, what is contested, and any claim leaned on harder "
            "than its evidence supports."
        ),
    },
    {
        "id": "dead_ends",
        "title": "Dead-ends & negative results",
        "charter": (
            "What did we rule out, and why? Build the negative-knowledge "
            "ledger from dead_end graph nodes, abandoned attempts and "
            "experiments, and needs_changes review histories: direction "
            "tested, setting, what happened, why it failed."
        ),
    },
    {
        "id": "coverage",
        "title": "Coverage & untested axes",
        "charter": (
            "What haven't we tried? Audit the project's stated intent against "
            "what experiments actually varied: which axes are cold, which look "
            "saturated, and where goals and actual exploration have drifted "
            "apart."
        ),
    },
)

CORE_LENS_IDS: tuple[str, ...] = tuple(lens["id"] for lens in CORE_LENSES)


SYNTHESIS_GATE_TABLE: dict[str, ForwardTransition] = {
    "reflecting": ForwardTransition(
        name="submit_reflections",
        to_status="synthesizing",
        requires_prose=(
            "every roster lens must have its own reflection synced & associated "
            "to this synthesis (role 'reflection') for the current attempt, in a "
            "file named <lens_id>.md — each reflection is authored and submitted "
            "by its own subagent"
        ),
        requirements=(
            RoleRequirement(
                role="reflection",
                error=(
                    "no reflections are associated yet: fan out one read-only "
                    "subagent per roster lens; each subagent writes its "
                    "reflection (e.g. syntheses/<syn_id>/reflections/"
                    "<lens_id>.md), registers it, and associates it with role "
                    "'reflection' for this synthesis"
                ),
                validator="roster",
                gate="reflection_roster_incomplete",
                action="fan_out_reflection_subagents",
                allowed=("resource.register_file", "resource.associate"),
                missing="one reflection per roster lens (role 'reflection')",
                guidance_key="reflection",
            ),
        ),
        ready_gate="reflections_complete",
        ready_action="submit_reflections",
        ready_allowed=("synthesis.transition",),
    ),
    "synthesizing": ForwardTransition(
        name="submit_synthesis",
        to_status="synthesis_review",
        requires_prose=(
            "the updated project logic graph (role 'graph', valid JSON DAG of at "
            "most 16 nodes) AND a what's-next proposals file (role 'proposals') "
            "must be synced & associated to this synthesis for the current "
            "attempt"
        ),
        requirements=(
            RoleRequirement(
                role="graph",
                error=(
                    "the project logic graph must be synced before "
                    "synthesis_review: update the living project graph (e.g. "
                    "project/logic_graph.json — the current logic state of the "
                    "whole project as a DAG of at most 16 nodes), register it, "
                    "and associate it with role 'graph' — see "
                    "skills/research-workflow/graph-template.md"
                ),
                validator="graph",
                gate="project_graph_required",
                action="update_and_associate_project_graph",
                allowed=("resource.register_file", "resource.associate"),
                missing="project logic graph resource (role 'graph')",
                guidance_key="project_graph",
            ),
            RoleRequirement(
                role="proposals",
                error=(
                    "a what's-next proposals file must be synced before "
                    "synthesis_review: write the next-wave experiment proposals "
                    "(each with a hypothesis, builds_on refs, and the claim it "
                    "would move), register it, and associate it with role "
                    "'proposals' — see "
                    "skills/research-reflection/synthesis-template.md"
                ),
                validator="prose",
                gate="proposals_required",
                action="write_and_associate_proposals",
                allowed=("resource.register_file", "resource.associate"),
                missing="what's-next proposals resource (role 'proposals')",
                guidance_key="proposals",
            ),
        ),
        ready_gate="synthesis_review_required",
        ready_action=(
            "submit_synthesis (call only once the project graph reflects the "
            "reconciled story and the proposals are real; if revision_context "
            "is present, the last review rejected this attempt — address it "
            "before resubmitting)"
        ),
        ready_allowed=("synthesis.transition",),
    ),
    "synthesis_review": ForwardTransition(
        name="publish",
        to_status="published",
        requires_prose="a passing synthesis_reviewer review",
        review=ReviewRequirement(
            role="synthesis_reviewer",
            skill="synthesis-review",
            action_name="synthesis_review",
            error="synthesis review must pass before publish",
            pass_action="publish_synthesis",
        ),
    ),
}


# (from_status, transition) -> next_status, derived so graph and gate
# contracts cannot diverge. No system transitions: nothing executes here.
SYNTHESIS_TRANSITION_GRAPH: dict[tuple[str, str], str] = {
    (status, forward.name): forward.to_status
    for status, forward in SYNTHESIS_GATE_TABLE.items()
}

SYNTHESIS_TRANSITION_REQUIREMENTS: dict[str, str] = {
    forward.name: forward.requires_prose
    for forward in SYNTHESIS_GATE_TABLE.values()
    if forward.requires_prose
}


def allowed_synthesis_transitions_for(status: str) -> list[dict[str, Any]]:
    """Agent-callable transitions available from ``status``, with hints."""
    if status in SYNTHESIS_TERMINAL_STATUSES:
        return []
    out: list[dict[str, Any]] = []
    for (frm, transition), nxt in SYNTHESIS_TRANSITION_GRAPH.items():
        if frm == status:
            entry: dict[str, Any] = {"transition": transition, "leads_to": nxt}
            if transition in SYNTHESIS_TRANSITION_REQUIREMENTS:
                entry["requires"] = SYNTHESIS_TRANSITION_REQUIREMENTS[transition]
            out.append(entry)
    out.append({"transition": "abandon", "leads_to": "abandoned"})
    return out
