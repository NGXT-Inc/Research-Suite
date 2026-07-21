"""Declarative gate table for the project reflection-wave workflow.

The workflow is one project-level reflection wave: a roster of differentiated reflection
lenses fans out, each lens submits its own reflection, the orchestrator
reconciles them into the living project logic graph, a concise reflection
document, and a belief-state change spec. A reflection reviewer judges all
three against the corpus. The FSM is deliberately smaller than the experiment
one — nothing executes, so there is no sandbox lifecycle:

    reflecting ──submit_reflections──▶ synthesizing ──submit_reflection_artifacts──▶
        reflection_review ──publish──▶ published

Review rejections route back through two distinct targets (mirroring the
experiment planned/running split): ``return_to='reflecting'`` re-launches the
fan-out (attempt bump — every lens must submit a fresh reflection), while
``return_to='synthesizing'`` keeps the reflections standing and only the
reflection artifacts (graph + reflection doc + change spec) are revised.

The same three consumers as ``domain.workflow_gates.GATE_TABLE`` read this table —
enforcement (``ReflectionService._next_status``), guidance
(``NextActionPolicy._reflection_workflow_for``), and discovery
(``allowed_reflection_transitions_for``) — reusing the same gate contract
dataclasses so the two workflows cannot drift in shape.

All gates here check envelopes only (files exist, the roster is covered, the
graph parses within budget, the reflection doc is concise, and the change spec
is materializable). Whether the reflection is honest and the proposed
belief-state update is warranted is the reviewer's call, and the diversity
heuristics live in the project-reflection skill, not in gates.
"""

from __future__ import annotations

from typing import Any

from merv.shared.artifact_roles import PROJECT_GRAPH_ROLE, REFLECTION_LENS_DOC_ROLE

from .gates import ForwardTransition, ReviewRequirement, RoleRequirement


REFLECTION_TERMINAL_STATUSES = frozenset({"published", "abandoned"})

# Engineered diversity: every wave fans out exactly five lenses — the three
# core ones below plus two the orchestrator designs for this specific project
# (each with a stated reason it is distinct). The full lens briefs live in
# skills/project-reflection; these charters are the durable one-line versions
# recorded on the roster when the agent does not supply its own wording.
ROSTER_SIZE = 5

CORE_LENSES: tuple[dict[str, str], ...] = (
    {
        "id": "amplify",
        "title": "Amplify what works",
        "charter": (
            "What worked, and what should we do more of? Identify positive "
            "signal, repeated wins, promising mechanisms, and directions where "
            "additional investment is justified."
        ),
    },
    {
        "id": "avoid",
        "title": "Avoid what failed",
        "charter": (
            "What did not work, and what should we avoid? Build the "
            "negative-knowledge ledger from dead_end graph nodes, abandoned "
            "attempts and experiments, and needs_changes review histories: "
            "direction tested, setting, what happened, why it failed."
        ),
    },
    {
        "id": "entropy",
        "title": "Entropy & weird bets",
        "charter": (
            "What unlikely, high-variance things should we try to escape the "
            "project's current local optimum? Generate strange but testable "
            "ideas, surprising pivots, and experiments the other lenses would "
            "probably dismiss too quickly."
        ),
    },
)

CORE_LENS_IDS: tuple[str, ...] = tuple(lens["id"] for lens in CORE_LENSES)


REFLECTION_GATE_TABLE: dict[str, ForwardTransition] = {
    "reflecting": ForwardTransition(
        name="submit_reflections",
        to_status="synthesizing",
        requires_prose=(
            "every roster lens must have its own reflection registered and associated "
            "to this reflection wave (role 'reflection_lens_doc') for the current "
            "attempt, in a file named <lens_id>.md — each reflection document "
            "is authored and submitted by its own subagent"
        ),
        requirements=(
            RoleRequirement(
                role=REFLECTION_LENS_DOC_ROLE,
                error=(
                    "no reflections are associated yet: fan out one read-only "
                    "subagent per roster lens; each subagent writes its "
                    "reflection (e.g. reflections/<syn_id>/reflections/"
                    "<lens_id>.md), registers it, and associates it with role "
                    "'reflection_lens_doc' for this reflection wave"
                ),
                validator="roster",
                gate="reflection_roster_incomplete",
                action="fan_out_reflection_subagents",
                allowed=("resource.register",),
                missing=(
                    "one reflection document per roster lens "
                    "(role 'reflection_lens_doc')"
                ),
                guidance_key="reflection",
            ),
        ),
        ready_gate="reflections_complete",
        ready_action="submit_reflections",
        ready_allowed=("reflection.transition",),
    ),
    "synthesizing": ForwardTransition(
        name="submit_reflection_artifacts",
        to_status="reflection_review",
        requires_prose=(
            "the updated project logic graph (role 'project_graph', valid JSON "
            "DAG of at most 16 nodes), a concise reflection document (role "
            "'reflection_doc'), AND a machine-actionable change spec (role "
            "'change_spec') must be registered and associated to this reflection wave for "
            "the current attempt; after approval, publish applies the claim "
            "changes and creates the next experiment wave"
        ),
        requirements=(
            RoleRequirement(
                role=PROJECT_GRAPH_ROLE,
                error=(
                    "the project logic graph must be registered before "
                    "reflection review: update the living project graph (e.g. "
                    "project/logic_graph.json — the current logic state of the "
                    "whole project as a DAG of at most 16 nodes), register it, "
                    "and associate it with role 'project_graph' — see "
                    "skills/research-workflow/graph-template.md"
                ),
                validator="graph",
                gate="project_graph_required",
                action="update_and_associate_project_graph",
                allowed=("resource.register",),
                missing="project logic graph resource (role 'project_graph')",
                guidance_key="project_graph",
            ),
            RoleRequirement(
                role="reflection_doc",
                error=(
                    "a concise reflection document must be registered before "
                    "reflection review: write the main agent's short markdown "
                    "reflection on the five lens reflections, register it, "
                    "and associate it with role 'reflection_doc' — see "
                    "skills/project-reflection/reflection-artifacts-template.md"
                ),
                validator="reflection_doc",
                gate="reflection_doc_required",
                action="write_and_associate_reflection_doc",
                allowed=("resource.register",),
                missing="reflection document resource (role 'reflection_doc')",
                guidance_key="reflection_doc",
            ),
            RoleRequirement(
                role="change_spec",
                error=(
                    "a change spec must be registered before reflection review: write "
                    "JSON with claim_changes plus a create_experiments decision "
                    "(1-3 experiments), register it, and "
                    "associate it with role 'change_spec' — see "
                    "skills/project-reflection/reflection-artifacts-template.md"
                ),
                validator="change_spec",
                gate="change_spec_required",
                action="write_and_associate_change_spec",
                allowed=("resource.register",),
                missing="change spec resource (role 'change_spec')",
                guidance_key="change_spec",
            ),
        ),
        ready_gate="reflection_review_required",
        ready_action=(
            "submit_reflection_artifacts (call only once the project graph reflects the "
            "reconciled reasoning state, the reflection doc explains the "
            "scientific argument concisely, and the change spec represents the "
            "intended belief-state update; if revision_context is present, the "
            "last review rejected this attempt — address it "
            "before resubmitting)"
        ),
        ready_allowed=("reflection.transition",),
    ),
    "reflection_review": ForwardTransition(
        name="publish",
        to_status="published",
        requires_prose="a passing reflection_reviewer review",
        review=ReviewRequirement(
            role="reflection_reviewer",
            skill="project-reflection-review",
            action_name="reflection_review",
            error="reflection review must pass before publish",
            pass_action="publish_reflection",
        ),
    ),
}


# (from_status, transition) -> next_status, derived so graph and gate
# contracts cannot diverge. No system transitions: nothing executes here.
REFLECTION_TRANSITION_GRAPH: dict[tuple[str, str], str] = {
    (status, forward.name): forward.to_status
    for status, forward in REFLECTION_GATE_TABLE.items()
}

REFLECTION_TRANSITION_REQUIREMENTS: dict[str, str] = {
    forward.name: forward.requires_prose
    for forward in REFLECTION_GATE_TABLE.values()
    if forward.requires_prose
}


def allowed_reflection_transitions_for(status: str) -> list[dict[str, Any]]:
    """Agent-callable transitions available from ``status``, with hints."""
    if status in REFLECTION_TERMINAL_STATUSES:
        return []
    out: list[dict[str, Any]] = []
    for (frm, transition), nxt in REFLECTION_TRANSITION_GRAPH.items():
        if frm == status:
            entry: dict[str, Any] = {"transition": transition, "leads_to": nxt}
            if transition in REFLECTION_TRANSITION_REQUIREMENTS:
                entry["requires"] = REFLECTION_TRANSITION_REQUIREMENTS[transition]
            out.append(entry)
    out.append({"transition": "abandon", "leads_to": "abandoned"})
    return out
