---
name: project-reflection
description: >-
  Use when the project should reflect across all of its experiments: distill
  what has actually been learned into the living 16-node project logic graph
  and submit a concise reflection document plus reviewed change spec. Orchestrates a
  roster of five differentiated reflection subagents through the gated
  reflection workflow (reflection.create → fan-out → reconcile → reflection
  review → publish).
  Invoke when the user asks for a project reflection, or when
  workflow.status_and_next nudges that the project graph has gone stale.
---

# Research Reflection

A reflection wave zooms out and thinks critically across the whole project:
what worked, what didn't, and what to do next. It reads everything — every
experiment, claim, review, and per-experiment logic graph — and produces
three reviewed reflection artifacts:

- the **project logic graph** (role `project_graph`): one living JSON file, the
  current *logic state* of the whole project — what is established, what was
  ruled out and why, what is open — within the same 16-node budget as
  experiment graphs;
- the **reflection document** (role `reflection_doc`): a short markdown
  scientific reading of the five lens reflections, written by the orchestrator;
- the **change spec** (role `change_spec`): the reviewed belief-state update
  — claim creations/updates plus the concrete next wave of planned
  experiments to create.

Quality comes from two mechanisms, both enforced by the workflow: **diversity
of thought** (five reflection agents, each reading the project from a
different angle, each submitting its own reflection) and **critique before
commit** (a separate reflection reviewer judges the result against the corpus
before anything is published).

You are the orchestrator. You own the reflection wave's transitions; you do
not write the reflections — the subagents do.

Pass the key-bound `project_id` on every project-scoped tool you call
(`reflection.*`, `review.request`, `artifact.submit`, …) — learn it once from
`project(action="current")` if you don't already have it. `review.start` and
`review.submit` are capability-addressed and take none.

## The workflow at a glance

```
reflection.create (declare the 5-lens roster; corpus is snapshotted)
  → reflecting:    fan out 5 lens subagents; project reads are read-only and
                   EACH submits its own reflection doc (role
                   'reflection_lens_doc', with its lens_id)
  → submit_reflections (blocked until every lens is covered)
  → synthesizing:  reconcile the reflections; update the living project
                   graph (role 'project_graph') + write the short reflection doc
                   (role 'reflection_doc') + write the change spec
                   (role 'change_spec')
  → submit_reflection_artifacts
  → reflection_review: launch the project-reflection-review agent (read-only)
  → publish        (or return_to 'synthesizing' / 'reflecting' on rejection)
```

One wave may be open at a time. `reflection.get` shows per-lens coverage,
`gate_checklist`, and `allowed_transitions`; `workflow.status_and_next` carries
the wave's gate guidance under `project_reflection` while it is open.

## Step 1 — declare the roster

To ground yourself in the project's current live state before opening the wave,
read `project` with `action: "overview"` (every claim and experiment, including
terminal ones). Once you call `reflection.create` the snapshotted corpus — not
this live read — is the authoritative reflection input.

Call `reflection.create` with exactly five lenses: the three **core** lenses
— `amplify`, `avoid`, and `entropy`, passed by id alone; the server fills in
their charters — plus **two you design for this specific project**. For
each authored lens give a `charter` (what angle it reads the project from)
and `why_distinct` (how it differs from the core three and from the other
authored lens). Pick authored lenses where this project's blind spots
actually are; a menu of starting points: methodological rigor, a
cross-experiment pattern hunt, cost/compute efficiency, a domain-specific
angle, or a classic coverage audit. The justification is required; whether
the lenses are genuinely distinct is something the reviewer will judge.

## Step 2 — fan out (one lens subagent per lens)

Spawn five subagents in parallel. Each gets:

- its lens brief (below, or the authored charter),
- **the list of the other four lenses running**, with the instruction to stay
  in its lane — anything squarely in another lens's charter is that agent's
  job, not yours to duplicate;
- **the wave's new signal** — `reflection.get`'s corpus lists
  `new_terminal_experiments`, the experiments that finished since the last
  published wave. Pass the list with this framing: these are why the project
  is reflecting now — they carry the signal the last wave never saw — but the
  job stays macro. Read the whole project through your lens and weigh the new
  results against everything that came before; do not narrow into a review of
  the new experiments;
- **for the three core lenses, the lens's previous reflection** —
  `corpus.previous_lens_reflections[<lens_id>]`, once a wave has published.
  Hand the path over as private context, not source material: it shows what
  this lens concluded last time, so the agent can learn from its own prior
  round — what held up, what broke, what deserves different weight now. The
  researcher sees only the current wave's reflection, so it must stand alone:
  no "as noted last wave", no references to the previous document, and no
  conclusion carried forward without re-verifying it against the current
  records. (Authored lenses are wave-specific and start fresh.);
- read-only project access (claims, experiments and their logic graphs,
  reports, reviews, artifacts — via MCP reads and repo files);
- the instruction to write its reflection to a local file (e.g.
  `reflections/<syn_id>/reflections/<lens_id>.md`), then submit it with
  `artifact.submit` (pass the `path`, `target_type: "reflection"`, the wave id
  as `target_id`, `role: "reflection_lens_doc"`, and `lens_id: "<lens_id>"` —
  coverage is matched by the explicit lens_id) and run the returned upload
  command verbatim — **the subagent submits its own reflection**; do not
  collect and submit on its behalf.

After the wave publishes, each lens may also register a distinct handle with
`feed.register` (`role="lens"`) and post ONE `feed.post` with its single
sharpest insight — the one thing from its angle most worth the researcher's
attention — in plain language (the feed-posting skill's one-turn test
applies). This is optional, one post per lens, not a summary of the whole
reflection.

The three core lens briefs:

> **Core 1 · `amplify` — Amplify what works: "where is the project getting
> traction, and how should we double down?"**
> Read the claims, experiment outcomes, reports, logic graphs, and review
> verdicts for positive signal. Identify what actually worked: repeated wins,
> promising mechanisms, surprisingly robust settings, productive methods,
> under-exploited partial successes, and places where more investment is
> justified. Name the evidence strength for each recommendation. Your job is
> not to summarize everything known; it is to find the strongest "do more of
> this" opportunities without overstating weak evidence.

> **Core 2 · `avoid` — Avoid what failed: "what should the project stop doing
> or avoid repeating?"**
> Read every `dead_end` node across experiment logic graphs, abandoned
> attempts and experiments, failed or inconclusive reports, and
> `needs_changes` review histories. Produce the negative-knowledge ledger as
> a table: direction tested · setting · what happened · why it failed · what
> would have to change before trying again. This is the project's
> highest-value memory — the thing that stops the next wave from re-running a
> known dead end. The ledger is cumulative: because the researcher reads only
> the current wave, re-verify still-binding rows from your previous
> reflection and carry them forward, and drop rows the records no longer
> support — the current table must stand alone.

> **Core 3 · `entropy` — Entropy & weird bets: "what strange, high-variance
> things should we try to escape the current local optimum?"**
> Deliberately inject unlikely-to-work ideas that might reveal a new axis.
> Look for weird mechanisms, surprising adjacent methods, contrarian pivots,
> cheap stress tests, uncomfortable assumptions to invert, and experiments
> the other agents would probably dismiss too quickly. Be bold, but every
> idea must be testable and scoped enough to become an experiment. Mark each
> idea with why it is unlikely, what it could reveal if it works or fails, and
> the cheapest decisive probe. Do not confuse entropy with vague brainstorming:
> the orchestrator and reviewer will filter your candidates before anything
> reaches the change spec.

When every lens has submitted, call
`reflection.transition(submit_reflections)`. The gate lists any lens still
missing.

## Step 3 — synthesize

Read all five reflections, then:

> Treat them as **unverified and possibly conflicting inputs, not as ground
> truth**. Where a reflection asserts something, check it against the actual
> records before you carry it forward. Your job is not to average or merge
> them — it's to **reconcile** them: surface what genuinely holds, name what
> they disagree on, and keep the eliminated avenues and partial progress, not
> just the wins.

Produce three artifacts and submit each to the reflection wave (artifact.submit):

1. **The project logic graph** (role `project_graph`) — edit the living file (e.g.
   `project/logic_graph.json`) in place; same envelope as experiment graphs
   (valid JSON `version: 1`, ≤16 nodes, DAG — see
   `skills/research-workflow/graph-template.md`). You design it: nodes are
   whatever the project's logic state needs — lessons, themes, dead-end
   patterns, open questions — in your own vocabulary. The budget forces
   pruning; retiring stale or superseded nodes or folding multiple nodes to make room is part of
   telling the current story. Node `refs` may point at `exp_` / `claim_` /
   `rev_` / `syn_` ids or repo files (reflections included), so keep nodes
   brief and link the detail.
2. **The reflection document** (role `reflection_doc`) — see
   `reflection-artifacts-template.md`. This markdown file is the orchestrator's compact
   critical reading of the five reflections. It should not be a long report:
   keep it under 16 KB, use the required sections (`Summary`, `Critical
   reading`, `Decision / future directions`), let refs carry detail, and use a
   few relative image links when a visual makes the reflection easier to read.
3. **The change spec** (role `change_spec`) — see
   `reflection-artifacts-template.md`. This JSON file is the project belief-state update:
   `claim_changes` creates or edits claims, and `decision` proposes the next
   wave: 1-3 concrete planned experiments, each with a folder-safe `name`, an
   `intent`, and tested claim refs; when the wave has more than one
   experiment, each also needs a `parallelism` note. Stopping the project is
   not a decision the reflection can make — that call belongs to the
   researcher. Use claim `key`s when a new claim created in
   `claim_changes` is referenced by an experiment in the same spec.

Do not create the experiments yourself during reflection. They are materialized
only when `reflection.transition(publish)` succeeds after reviewer approval.

Before requesting review, call `reflection.get` and inspect `gate_checklist`.
It should show valid `project_graph`, `reflection_doc`, and `change_spec`
items before you transition. Also inspect `project_graph_diff` when it is
available. Use it as a compact previous-vs-new check: what nodes/edges were
added, pruned, or changed relative to the last published project graph.

Then `reflection.transition(submit_reflection_artifacts)`.

## Step 4 — review

Request the review with
`review.request(project_id=<project_id>, target_type='reflection',
target_id=<syn_id>, role='reflection_reviewer',
producer_session_id=<your session>)`. The
response's `reviewer_handoff.spawn_prompt` is a ready-made prompt for
spawning the **separate** read-only `project-reflection-review` agent — use
it rather than assembling the handoff yourself (the producer session cannot
start the review). The capability plaintext appears only in this request
response, but `review.start` does not consume it; do not supersede the open
request merely because the reviewer started. Reviewer read-only behavior is a
procedural rule: the capability authenticates `review.start` and the returned
session authenticates `review.submit`, but unrelated tool calls are not
authenticated as reviewer calls. The reviewer sees the corpus, the previous
graph, all five reflections, and your reflection artifacts — and verdicts route:

- `pass` → call `reflection.transition(publish)`. The wave pins the graph
  version it published, applies the approved claim changes, and creates the
  approved planned experiments. Read `post_publish_guidance`: it names the new
  experiment folders; create those directories yourself before writing into
  them because there is no `experiment.materialize_folders` tool. The living
  graph file remains for the next wave to edit.
- `needs_changes` with `return_to: "synthesizing"` → the reflections stand;
  revise the graph, reflection doc, and/or change spec and resubmit.
- `needs_changes` with `return_to: "reflecting"` → the attempt bumps and
  every lens owes a fresh reflection: re-run Step 2 (address what the review
  found — typically lens overlap or a missed angle).

## Keeping the project graph alive

After publishing, the project graph is the project's current logic state and
the UI shows it on the Home page. `workflow.status_and_next` computes drift —
experiments finishing, claims flipping — and will nudge ("Consider running a
project reflection…") when the published reflection has fallen behind. Past the
hard experiment threshold, `experiment.create` is blocked until a reflection is
published; publish creates the reviewed next experiment wave.
