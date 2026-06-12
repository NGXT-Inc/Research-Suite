---
name: research-reflection
description: >-
  Use when the project should reflect across all of its experiments: distill
  what has actually been learned into the living 16-node project logic graph
  and propose the next wave of experiments. Orchestrates a roster of five
  differentiated reflection subagents through the gated synthesis workflow
  (synthesis.create → fan-out → synthesize → synthesis review → publish).
  Invoke when the user asks for a project reflection/synthesis, or when
  workflow.status_and_next nudges that the project graph has gone stale.
---

# Research Reflection (project synthesis)

A reflection wave reads the whole project — every experiment, claim, review,
and per-experiment logic graph — and produces two reviewed artifacts:

- the **project logic graph** (role `graph`): one living JSON file, the
  current *logic state* of the whole project — what is established, what was
  ruled out and why, what is open — within the same 16-node budget as
  experiment graphs;
- the **what's-next proposals** (role `proposals`): the experiments the next
  wave should run, grounded in that state.

Quality comes from two mechanisms, both enforced by the workflow: **diversity
of thought** (five reflection agents, each reading the project from a
different angle, each submitting its own reflection) and **critique before
commit** (a separate synthesis reviewer judges the result against the corpus
before anything is published).

You are the orchestrator. You own the synthesis record's transitions; you do
not write the reflections — the subagents do.

## The workflow at a glance

```
synthesis.create (declare the 5-lens roster; corpus is snapshotted)
  → reflecting:    fan out 5 read-only subagents; EACH submits its own
                   reflection (role 'reflection', file <lens_id>.md)
  → submit_reflections (blocked until every lens is covered)
  → synthesizing:  reconcile the reflections; update the living project
                   graph (role 'graph') + write proposals (role 'proposals')
  → submit_synthesis
  → synthesis_review: launch the synthesis-review agent (read-only)
  → publish        (or return_to 'synthesizing' / 'reflecting' on rejection)
```

One wave may be open at a time. `synthesis.get` shows per-lens coverage and
`allowed_transitions`; `workflow.status_and_next` carries the wave's gate
guidance under `project_reflection` while it is open.

## Step 1 — declare the roster

Call `synthesis.create` with exactly five lenses: the three **core** lenses
below (pass just their ids — charters are filled in) plus **two you design
for this specific project**. For each authored lens give a `charter` (what
angle it reads the project from) and `why_distinct` (how it differs from the
core three and from the other authored lens). Pick authored lenses where this
project's blind spots actually are; a menu of starting points:
methodological rigor, a cross-experiment pattern hunt, cost/compute
efficiency, a domain-specific angle, an explicit devil's advocate. The
justification is required; whether the lenses are genuinely distinct is
something the reviewer will judge.

## Step 2 — fan out (one read-only subagent per lens)

Spawn five subagents in parallel. Each gets:

- its lens brief (below, or the authored charter),
- **the list of the other four lenses running**, with the instruction to stay
  in its lane — anything squarely in another lens's charter is that agent's
  job, not yours to duplicate;
- read-only project access (claims, experiments and their logic graphs,
  reports, reviews, resources — via MCP reads and repo files);
- the instruction to write its reflection to
  `syntheses/<syn_id>/reflections/<lens_id>.md` (the filename **must** be
  `<lens_id>.md` — coverage is matched by filename), then
  `resource.register_file` it and `resource.associate` it to the synthesis
  with role `reflection` — **the subagent submits its own reflection**; do
  not collect and submit on its behalf.

The three core lens briefs:

> **Core 1 · `outcomes` — Outcomes & evidence: "what do we actually know?"**
> Read the claims (supported / weakened / contradicted / active), experiment
> outcomes, and review verdicts. Assemble the *verified* knowledge state:
> what's established, what's contested, and — critically — any claim being
> leaned on harder than its evidence supports. You are the verification
> lens; do not speculate about untried directions — that's another agent's
> job.

> **Core 2 · `dead_ends` — Dead-ends & negative results: "what did we rule
> out, and why?"**
> Read every `dead_end` node across the experiment logic graphs, abandoned
> attempts and experiments, and `needs_changes` review histories. Produce the
> negative-knowledge ledger as a table: direction tested · setting · what
> happened · why it failed. This is the project's highest-value memory — the
> thing that stops the next wave from re-running a known dead end.

> **Core 3 · `coverage` — Coverage & untested axes: "what haven't we
> tried?"**
> Compare the project's stated intent against what experiments actually
> varied. Run a coverage audit: which axes are cold (touched by few or no
> experiments), which look saturated (recent variation below the noise the
> experiments themselves report), and where the project's goals and its
> actual exploration have drifted apart. You map the frontier; you don't
> adjudicate past results.

When every lens has submitted, call
`synthesis.transition(submit_reflections)`. The gate lists any lens still
missing.

## Step 3 — synthesize

Read all five reflections, then:

> Treat them as **unverified and possibly conflicting inputs, not as ground
> truth**. Where a reflection asserts something, check it against the actual
> records before you carry it forward. Your job is not to average or merge
> them — it's to **reconcile** them: surface what genuinely holds, name what
> they disagree on, and keep the eliminated avenues and partial progress, not
> just the wins.

Produce two artifacts and associate each to the synthesis:

1. **The project logic graph** (role `graph`) — edit the living file (e.g.
   `project/logic_graph.json`) in place; same envelope as experiment graphs
   (valid JSON `version: 1`, ≤16 nodes, DAG — see
   `skills/research-workflow/graph-template.md`). You design it: nodes are
   whatever the project's logic state needs — lessons, themes, dead-end
   patterns, open questions — in your own vocabulary. The budget forces
   pruning; retiring stale or superseded nodes to make room is part of
   telling the current story. Node `refs` may point at `exp_` / `claim_` /
   `rev_` / `syn_` ids or repo files (reflections included), so keep nodes
   brief and link the detail.
2. **The proposals file** (role `proposals`) — see
   `synthesis-template.md`. For the proposal set, as guidance rather than
   rules: each proposal carries a hypothesis, `builds_on` refs, and the claim
   it would move; include at least one non-incremental proposal *or* state
   why none exists; don't re-enter a direction from the dead-end ledger
   without saying what's different this time; and when a past direction won,
   consider one proposal probing *why* it worked through a different
   mechanism.

Then `synthesis.transition(submit_synthesis)`.

## Step 4 — review

Request the review with
`review.request(target_type='synthesis', target_id=<syn_id>,
role='synthesis_reviewer', producer_session_id=<your session>)` and hand the
capability to a **separate** read-only `synthesis-review` agent (the producer
session cannot start it). The reviewer sees the corpus, the previous graph,
all five reflections, and your synthesis — and verdicts route:

- `pass` → call `synthesis.transition(publish)`. The wave pins the graph
  version it published; the living file remains for the next wave to edit.
- `needs_changes` with `return_to: "synthesizing"` → the reflections stand;
  revise the graph/proposals and resubmit.
- `needs_changes` with `return_to: "reflecting"` → the attempt bumps and
  every lens owes a fresh reflection: re-run Step 2 (address what the review
  found — typically lens overlap or a missed angle).

## Keeping the project graph alive

After publishing, the project graph is the project's current logic state and
the UI shows it on the Home page. `workflow.status_and_next` computes drift —
experiments finishing, claims flipping — and will nudge ("Consider running a
project reflection…") when the published synthesis has fallen behind. The
nudge is advisory: whether new developments change the project's logic state
is your editorial call.
