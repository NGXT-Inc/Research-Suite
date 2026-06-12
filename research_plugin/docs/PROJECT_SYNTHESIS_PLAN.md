# Project Synthesis & Reflection — Build Plan

**Status:** implemented (backend + skills + UI), 2026-06-11. Open questions §9
were taken at their noted leanings: redo_reflection re-runs all five lenses
(the reviewer's notes can tell the orchestrator which were weak), and
proposals are their own role-`proposals` file.
**Authored:** 2026-06-11.
**Scope:** a new project-level, gated reflection workflow that periodically distills the whole
project into one living 16-node "project logic graph" plus a "what's next" proposal set, via a
fan-out of role-differentiated reflection agents, gated by a synthesis review.

This document is the durable record of the design conversation: the bottleneck it solves, the
lessons drawn from two research papers, the user's intent in their own words, the decisions taken,
the full workflow design (FSM, gates, roster, skill prompts, backend lift), and an explicit
mechanism for tracking when the project logic graph was last updated so the system can nudge when
it has gone stale.

---

## 1. The bottleneck this solves

The plugin is already good at keeping a single experiment on rails: workflows, gates, hints during
MCP interactions, and skills all do their job. Each experiment also now ships an **agent-authored
logic graph** (role `graph`, ≤16 nodes, free-form, with server-resolved node `refs` → links) that
tells the story of how that one experiment went.

What is missing is a level up: a way for agents to **reflect across all experiments** — to identify
what the project has actually learned and how to extend it into the next set of ideas/experiments to
build from. Today that high-level "logic state of the project" lives only implicitly, scattered
across per-experiment artifacts. We want it to be a first-class, maintained, reviewed object.

The design principle the user fixed: **this project-level 16-node graph is the current "logic state"
of the project** and is "almost load bearing." The experiment table remains the structured,
navigable list; the project graph is the synthesized narrative state on top of it. It deserves
disproportionate attention and is allowed to justify new database tables.

---

## 2. What we build on (current repo)

Primitives this design reuses rather than reinvents:

- **GATE_TABLE pattern** (`backend/services/workflow_gates.py`): a declarative single source of truth.
  Each status has one `ForwardTransition` carrying `RoleRequirement`s (role, validator, gate, action,
  error, guidance) and/or a `ReviewRequirement`. It is consumed by three call sites — enforcement
  (`_next_status`), guidance (`_workflow_for`), and discovery (`allowed_transitions_for`). Adding a
  second small gate table for syntheses gives us enforcement/guidance/discovery for free.
- **State machine + return routing**: experiments run planned → design_review → ready_to_run →
  running → experiment_review → complete, with review rejections routed back via `return_to`
  (planned bumps the attempt; running keeps it). The two-target return we need here mirrors this.
- **Resource model**: one repo file = one resource keyed `(project_id, path)`, observation-only
  (mtime/ctime/size/sha256), attempt-stamped associations; gates count only current-attempt
  associations. Reflections and the project graph are plain repo files under this model.
- **Logic-graph envelope lint** (`backend/services/graph_lint.py`): leaf module; `graph_problems()`
  enforces valid JSON `version:1`, non-empty nodes with unique `id` + non-empty `label`, ≤16 nodes,
  edges referencing existing nodes, no self-loops, acyclic, ≤16 KB. `kind`/`detail`/`refs`/extras are
  free-form and ignored. **This lint is reused verbatim for the project graph.**
- **Node `refs` → `ref_index` resolution** (`backend/http_api.py`): node `refs` are plain strings,
  resolved server-side on read into links for resource (`res_`/path), review (`rev_`), claim
  (`claim_`), experiment (`exp_`). Verified live across all types. We extend it with `syn_`.
- **Review capabilities**: one-time tokens, `target_snapshot_id` pinning, read-only reviewer funnel
  (`reject_reviewer_mutation`), producer-session rejection, `return_to` routing. Reused for the
  synthesis review, with a new target kind.
- **UI**: React + Vite, ReactFlow canvas with `layoutFigure`, the `LogicGraph` component (renders the
  graph endpoint, the JSON-string identity trick, `MeasureSync`, polling), now hosted in
  `ExperimentGraphs` (Figure/Logic tabs share one canvas slot). The project graph renders through the
  same `LogicGraph` component against a project-scoped endpoint.

**Framing insight:** the per-experiment logic graph IS the PDR "bounded workspace" primitive, one
level down. The project synthesis applies the same improvement operator one level up — parallel
reflections → distilled bounded graph → refined proposals → review → repeat.

---

## 3. Lessons from the research papers

Two papers were read in full (including appendices) to ground the design.

### 3.1 Rethinking Thinking Tokens: LLMs as Improvement Operators (arXiv 2510.01123, "PDR")

Treats the model as an **improvement operator** run in rounds:
**Parallel** (generate M diverse drafts) → **Distill** (compress into a bounded workspace
`C`, `|C| ≤ κ` tokens) → **Refine** (produce an improved artifact conditioned on `C`); repeat. SR
(Sequential Refinement) is PDR with parallelism 1. At matched sequential budget, PDR/SR beat a single
long chain-of-thought; PDR gives the largest gains (+11% AIME 2024, +9% AIME 2025).

Lessons we apply:

1. **Bounded & re-synthesized beats append-only.** Replaying all prior attempts recreates
   long-context failure modes and anchoring bias. The workspace is re-synthesized fresh each round
   and retires stale/contradicted information. → Our project graph is a bounded (16-node) workspace;
   the budget forces pruning, which *is* the feature.
2. **Verification before admission is load-bearing.** The Oracle experiment (Fig 6/8) shows admitting
   only *incorrect* drafts into the workspace causes large accuracy drops, while admitting only
   *correct* ones improves over baseline. The distill step must be a **verifier-aware aggregator**,
   not a vote-counter. → The synthesizer must reconcile reflections against the actual records (claim
   statuses, review verdicts, experiment graphs), not merge them on faith.
3. **Drafts are explicitly "unverified."** The real prompts (appendix B) tell the synthesizer: "Treat
   the summary as unverified; use it as context; come up with a better answer without starting from
   scratch." → Our synthesis prompt uses this exact framing for the N reflections.
4. **Distill must do more than summarize** (appendix E): surface the signal that distinguishes a
   correct minority among many distractors (3 correct vs 29 wrong), and when *no* draft is correct,
   extract partial progress / contradictions / **eliminated avenues** and expand diversity. → Our
   synthesis keeps dead ends and ruled-out directions, not just wins.
5. **Four meta-skills** the operator needs: verification, refinement, compression, diversification.
   These map onto our lenses and the synthesis/review split.

### 3.2 AUTOSCIENTISTS: Self-Organizing Agent Teams for Long-Running Scientific Experimentation (arXiv 2605.28655)

Decentralized agent teams coordinated through a **shared state** (champion, experiment log, shared
forum, dead-end registries) rather than a central planner. Two roles: analysts (maintain knowledge,
audit coverage, propose) and experiment agents (run, gate, record). Discussion (self-organize into
teams around directions) alternates with execution; re-discussion is **stagnation-triggered**.

Lessons we apply:

1. **Negative knowledge is the single highest-value artifact.** In the "from-champion" regime, the
   system seeded with `EXPLORED.md` (dead ends) found **7 accepted improvements where the single
   agent found 0 in 100 attempts**, and the first improvement was in a direction the single agent
   never proposed. → A dedicated **dead-ends lens** and a dead-end ledger are core, not optional.
2. **Diversity must be engineered, not assumed.** The `independent-agents` ablation (D.4): six solo
   agents with no shared surface had **five of six independently rediscover the same dominant
   first-axis win, wasting ~1/3 of the budget on duplicates.** → N undifferentiated reflectors
   converge on the obvious. We mandate distinct lenses + an anti-overlap rule (each agent is told the
   other lenses running and to stay in its lane). **This is the core lesson the user named: diversity
   of thought matters.**
3. **Cross-agent visibility of reasoning (not just outputs) matters.** The `no-cross-agent` ablation
   (D.2): allowing only PROPOSAL/RESULT posts (no critique/gap-analysis) needed **1.85× more
   experiments**. → The synthesis step reads the full reflections, and the reviewer sees all of them.
4. **The four mechanisms fix different failure modes; none dominates.** Proposal quality, critique-
   before-compute, reorganization, and the shared record each fail differently when removed. → Our
   design touches all four (lenses = proposal/coverage quality; review = critique; living graph =
   shared record; re-run paths = reorganization).
5. **Analyst proposal protocol (A.7) → the "what's next" backbone:**
   - *baseline coverage audit*: enumerate axes/parameters never varied.
   - *empirical axis priors*: mean effect size per (axis, direction); axes with few experiments are
     "cold" → exploration bonus; below the noise floor → deprioritize.
   - *ambition quota*: ≥1 non-incremental proposal, or an explicit stated reason none exists.
   - *diversity constraints*: proposals target different directions; nothing re-enters a dead end
     without stating what differs.
   - *post-win follow-up*: when something worked, ≥1 proposal follows up on *why it worked* via a
     different mechanism ("which property made it work, and what else shares that property?").
6. **Output artifact shapes to reuse**: the "research insights" document (directions tried / accepted
   / rejected + mechanism) and the **dead-ends table** (direction · setting · result · Δ · why it
   failed). The noise-aware promotion gate (A.6, Δ > Mσ or second-seed confirm) is the discipline
   behind not over-trusting a single reflection.

### 3.3 One-line synthesis

PDR says *how* to run the improvement operator (parallel → bounded distill → refine, verifier-aware,
unverified inputs). AutoScientists says *what content* makes it work at the project level (engineered
diversity, negative knowledge, coverage audits, ambition + diversity constraints on proposals).

---

## 4. User intent — exact messages (verbatim)

These are the user's own words across the design conversation, preserved for fidelity.

**(A) Framing the bottleneck and the reading task:**

> So at this point, we have gotten pretty good at making sure the agents are doing what they are
> supposed to, our workflows, gates, are hints during plugin interactions and skills are all doing
> their job.
>
> One bottleneck -> we need a good way for agents to reflect on all the high-level ideas and
> approaches taken in the project to identify what we can learn and how to extend the project into the
> next set of ideas/experiments to build from.
>
> Let's brainstorm how we might do that.
>
> Read this paper called auto-scientists, and this paper called Rethinking Thinking Tokens. The latter
> paper talks about how to generate several directions in parallel, ingest & synthesize them, and then
> the next wave of parallel generation - all in the name of improving end output. The former paper is a
> paper on orchestrating agents to do research. Let's see what relevant ideas to this task we can learn
> from them, too.

**(B) Asking for a gated workflow proposal:**

> Okay. Should we make this a gated workflow? Similar to how we have a workflow for each experiment?
>
> Propose something for me.

**(C) Approving the direction and fully specifying the workflow:**

> I quite like this. This system becomes almost load bearing in the project. While we will retain a
> structured list of experiments that the agent can pick and navigate to through the table structure,
> this project level 16 node knowledge graph serves as the current "logic state" of the project.
> Therefore, it is important that we pay a lot of attention to it. If needed, we can built new database
> tables to accomodate it.
>
> I like your gated workflow proposal and the outline to use skills to direct the agent how to fan out
> the parallel thinking and reflection on the project. This is the core lesson from the research papers
> -> diversity of thought matters. Re-read those papers and figure out the right way to write the skill
> to prompt the agents. We should be specific about the number of agents we need to fan out to, most
> likely-each agent reflecting should have a role or an id. Let's say that we want 3, or 5 agents, all
> looking at the repo from a different angle, we should require that each role submits its own
> reflection (this should be a hard requirement). Only once all the agents have submitted their
> reflections, can go into the synthesize step where the main agent reads the submissions, and submits
> an overall reflection + updated graph, and the "what's next" kind of a thing. Then, the reviewer
> reviews the reflection and graph based on 1) project information (it can gather information it needs
> through read-only access) 2) previous state of the graph, if available 3) The reflections of N agents
> (let's start with 3), and 4) the synthesized reflection and updated graph. The review agent can
> accept the result, or send the process back to the beginning where the agent would have to re-launch
> sub-agents for reflection, or send the process back to synthesis stage, where the main agent needs to
> change the submission, but doesn't have to re-run the sub-agents for reflection.
>
> Any questions about this?

**(D) The save request (this document's trigger):**

> I need you to save the plan of building this in a file. Describe all the lessons we learned from the
> research papers, and our current repo and then save everything including my answers, the messages I
> sent you (exact words) and how you planned out the workflow. Add a note to make sure that we keep
> track of when the project logic graph was last updated so that if there is a lot change/events since
> then, we can recognize that and nudge the agent when needed.

---

## 5. Decisions (the three forks and the user's answers)

Three schema-determining forks were put to the user; their answers:

1. **Graph lifecycle → "One living graph."** A single project graph that each reflection wave edits
   in place; the 16-node budget forces pruning of stale nodes. Each *published* synthesis record pins
   the graph **version token** it produced, so immutable history is preserved on top of the single
   living file.

2. **Reflection lenses → "Base 3 core lenses and 2 more added by the agent itself."** Five reflections
   total: three mandated, orthogonal core lenses (guaranteeing the engineered diversity the ablations
   require) plus two lenses the orchestrator designs for this specific project. The agent must justify
   why each added lens is distinct from the core three and from each other.

3. **Who submits reflections → "Each subagent submits itself."** The orchestrator fans out five
   read-only subagents; **each subagent registers + associates its own reflection file** under its
   lens id. The orchestrator owns only the FSM transitions.

---

## 6. The workflow design

### 6.1 Entities and artifacts

- **`synthesis` record** (`syn_…`), project-scoped. One per reflection wave. The latest `published`
  one is "the current synthesis." Carries: the declared **roster** (5 lens ids + charters), the
  **corpus snapshot** (terminal experiments at id+attempt at create time), attempt index, status,
  and — on publish — the pinned project-graph version token.
- **`reflection`** (new resource role): one per lens, a prose repo file at
  `syntheses/<syn_id>/reflections/<lens_id>.md`, authored and submitted by its own subagent.
- **project `graph`** (reuse role `graph`): the single living project logic graph file (e.g.
  `project/logic_graph.json`). Same JSON shape, same `graph_lint`, same 16-node budget, same `refs`
  resolution as experiment graphs. Nodes are lessons / themes / dead-end patterns / open questions.
- **`proposals`** (new resource role): the "what's next" file — one block per proposed experiment
  (hypothesis, `builds_on` refs, which claim it would move). Its own file so the *next* wave and
  `experiment_create` can link to it. (Open question 2 — leaning own file.)

### 6.2 FSM (four states, two distinct return paths)

```
reflecting ──all 5 submitted──▶ synthesizing ──graph+proposals──▶ synthesis_review ──pass──▶ published
    ▲                                  ▲                                  │
    └──── redo_reflection ─────────────┴──── redo_synthesis ─────────────┘
        (re-launch the fan-out)            (rewrite synthesis only;
                                            reflections stand)
```

- `abandoned` is the escape hatch (mirrors experiments).
- The two return paths are distinct transitions with distinct `return_to` targets — exactly the
  planned-vs-running return routing the codebase already has, generalized.
- Each rejection carries a soft `revision_context` ("Consider revising the synthesis / graph /
  proposals to …" — never "Update …"), consistent with house style.

### 6.3 Gates (declarative, second gate table)

- **`reflecting → synthesizing`** (`submit_reflections`): a new **roster-coverage validator** — every
  declared lens id must have an associated role-`reflection` resource for the current attempt. This is
  the hard "only once all N have submitted" requirement. Blocked message lists the missing lenses.
- **`synthesizing → synthesis_review`** (`submit_synthesis`): two `RoleRequirement`s —
  - role `graph`, validator `graph` (reuse `graph_lint`), and the living project graph must have been
    updated this wave (its version token advanced, or re-associated for the current attempt);
  - role `proposals`, validator `prose` (dumb existence + non-empty).
  Plus a `ReviewRequirement` for role `synthesis_reviewer`.
- All gates check **envelopes only**. Substance (is the story honest? are proposals real?) is the
  reviewer's call. Diversity heuristics live in the skill, not the gates.

### 6.4 Roles, resolution, surface (additions)

- `RESOURCE_ROLES += {"reflection", "proposals"}` (role `graph` reused for the project graph).
- Ref resolver `+= "syn_"` so experiment graphs (and the project graph) can point back at the
  synthesis that motivated them.
- MCP surface: `synthesis_create`, `synthesis_get`, `synthesis_list`, `synthesis_transition`.
- HTTP: `GET /projects/{pid}/syntheses`, `…/syntheses/current/graph` (renders via existing
  `LogicGraph`), `…/syntheses/{syn_id}`.

### 6.5 Backend lift, honestly sized

- New `syntheses` table + a second small gate table reusing `ForwardTransition` / `RoleRequirement` /
  `ReviewRequirement` dataclasses.
- New roster-coverage validator.
- **The one genuinely invasive change:** associations and reviews are experiment-scoped today. They
  need a `target_kind` (`experiment` | `synthesis`) and the review request/start/submit path needs
  target polymorphism. Everything else is additive.
- UI: a "Project synthesis" panel on Home rendering the living project graph through `LogicGraph`,
  plus the FSM strip and the staleness/coverage badge (§8).

---

## 7. The skill — `research-reflection`

A user-invocable skill (plus a soft nudge from `workflow_status_and_next`; see §8). It orchestrates
the wave; it does **not** gate process — only the artifacts are gated, in keeping with the plugin's
three-layer split (gates check envelopes, skills shape quality, reviewers judge substance).

**Step 1 — declare the roster.** `synthesis_create` snapshots the corpus and registers 5 lenses: the
3 core below + 2 the agent authors. For each authored lens the agent states *why it is distinct* — a
required field, the diversity constraint enforced socially rather than by a gate.

**Step 2 — fan out (read-only subagents, one per lens).** Each subagent gets its lens brief, the list
of the *other* lenses running (anti-overlap — the fix for the 1/3-wasted-budget failure), read-only
project access, and instructions to **submit its own reflection** (register + associate the file under
its lens id). The three mandated briefs:

> **Core 1 · Outcomes & evidence — "what do we actually know?"**
> Read the claims (supported / weakened / contradicted / active), experiment outcomes, and review
> verdicts. Assemble the *verified* knowledge state: what's established, what's contested, and any
> claim being leaned on harder than its evidence supports. You are the verification lens; do not
> speculate about untried directions — that's another agent's job.

> **Core 2 · Dead-ends & negative results — "what did we rule out, and why?"**
> Read every `dead_end` node across the experiment logic graphs, abandoned attempts and experiments,
> and `needs_changes` review histories. Produce the negative-knowledge ledger as a table: direction
> tested · setting · what happened · why it failed. This is the project's highest-value memory — the
> thing that stops the next wave from re-running a known dead end.

> **Core 3 · Coverage & untested axes — "what haven't we tried?"**
> Compare the project's stated intent against what experiments actually varied. Run a coverage audit:
> which axes are cold (touched by few or no experiments), which look saturated (recent variation below
> the noise the experiments themselves report), and where the project's goals and its actual
> exploration have drifted apart. You map the frontier; you don't adjudicate past results.

The two authored lenses come with a menu, not a mandate (methodological-rigor, cross-experiment
pattern, cost/resource, a domain-specific angle, an explicit devil's-advocate lens) and the
requirement that each be justified as distinct. Reflections are prose files (dumb existence +
non-empty lint).

**Step 3 — synthesize (verifier-aware prompt; wording is load-bearing, from PDR appendix B):**

> You have five reflections, each from an agent that saw the project through a single lens. **Treat
> them as unverified and possibly conflicting inputs, not as ground truth.** Where a reflection asserts
> something, check it against the actual records before you carry it forward. Your job is not to
> average or merge them — it's to reconcile them: surface what genuinely holds, name what they disagree
> on, and keep the eliminated avenues and partial progress, not just the wins. Then produce two things:
> the updated project logic graph (the current logic state — edit the living graph, prune within the
> 16-node budget), and the what's-next proposals.

**Step 4 — "what's next" protocol** (AutoScientists A.7 as guidance, not gates): each proposal carries
a hypothesis, `builds_on` refs, and which claim it would move; the set includes ≥1 non-incremental
proposal *or* a stated reason none exists (ambition quota); no proposal re-enters a dead end from Core
2's ledger without saying what's different; when a past direction won, ≥1 proposal follows up on *why
it worked* via a different mechanism (post-win follow-up).

**Step 5 — review.** A separate `synthesis-reviewer` session (producer-session rejection applies; same
capability machinery as experiment review) receives the four inputs the user specified:
1. project information (gathered through read-only access),
2. the previous project-graph version (pinned), if any,
3. the five reflections,
4. the synthesized graph + proposals.
It checks: does the synthesis reconcile with the corpus, or is anything cherry-picked or a dead end
retold as a win? Do proposals collide with the ledger unjustified? Is there real ambition? The graph's
vocabulary and structure are the author's design, not the reviewer's to prescribe. Verdicts route to
`pass` / `redo_synthesis` (reflections stand) / `redo_reflection` (re-launch the fan-out).

---

## 8. Tracking graph freshness and nudging (explicit user request)

> "keep track of when the project logic graph was last updated so that if there is a lot change/events
> since then, we can recognize that and nudge the agent when needed."

The reflection wave is **stagnation/▒change-triggered, not scheduled** (AutoScientists' re-discussion
trigger). To make "a lot of change since last update" recognizable, track and compare two anchors.

**What to record:**
- On the living project `graph` resource: `last_updated_at` (its mtime/version is already observed)
  and the version token. This is "when the project logic graph was last meaningfully edited."
- On each `published` synthesis: `published_at` and the **corpus snapshot** it covered (set of
  terminal experiments at id+attempt, and the claim/review state at that time).

**The staleness signal (computed server-side, no new writes needed):** since the last published
synthesis (or since `graph.last_updated_at`), count the project events that should make the agent
reconsider the logic state:
- experiments newly reaching a terminal / review-passed state,
- claims changing status — weighted heavily for flips to `contradicted` or `weakened`,
- reviews resolving `needs_changes` / `fail`,
- plus a **coverage delta**: `|current terminal experiments| − |covered by current synthesis|`.

**The nudge (soft, "Consider…", never forced):** when the signal crosses a threshold,
`workflow_status_and_next` and a Home **staleness/coverage badge** surface a hint, e.g.:

> "Consider running a project reflection — the project logic graph was last updated N days ago; X
> experiments have completed and Y claims have changed since (including a claim now contradicted).
> It covers 5 of 8 completed experiments."

Suggested starting threshold (tunable): **≥3 newly-terminal experiments since publish, OR any claim
flipped to `contradicted`, OR the graph older than the team's cadence.** Always advisory — it is the
agent's editorial call whether the new developments change the project's logic state, exactly as with
per-experiment graphs ("if a development adds no valuable information, it may leave it out").

This same freshness anchor doubles as the within-wave reminder: if the agent edits experiments but
not the project graph during a wave, the gap between `graph.last_updated_at` and project activity is
visible, and the soft reminder applies one level up from the existing experiment-graph reminder.

---

## 9. Open questions (carried, leaning noted)

1. **`redo_reflection` granularity** — when the reviewer bounces to `reflecting`, do all five subagents
   re-run, or may the reviewer name which lenses were weak so only those re-run? *Leaning: selective
   re-run, default-all.*
2. **Proposals file vs. section** — own `proposals` file, or a required section of the synthesis
   report? *Leaning: own file (linkable, queryable, consumed by the next wave).*

---

## 10. Build order

1. Backend: `syntheses` table + second gate table (FSM + roster-coverage validator) + association/
   review `target_kind` polymorphism. *(Skill and UI both bind to this surface.)*
2. Roles (`reflection`, `proposals`), ref resolver `syn_`, MCP surface, HTTP endpoints.
3. Freshness/staleness signal + nudge in `workflow_status_and_next` (§8).
4. Skill `research-reflection` + `synthesis-template.md` + `synthesis-review` agent/skill.
5. UI: Home "Project synthesis" panel (reuse `LogicGraph`) + FSM strip + staleness/coverage badge.
6. Tests at each layer (gate enforcement, roster coverage, review polymorphism, staleness signal).

---

## 11. Provenance note

Per-experiment logic-graph **refs** (node `refs` → server-resolved `ref_index` → UI links) are the
foundation this builds on and are **complete and verified** (288 backend tests green; all five ref
kinds — resource by path, resource by `res_` id, claim, experiment, review, plus unresolved
degradation — confirmed live in the preview). Extending the resolver with `syn_` (step 2 above) lets
experiment graphs and the project graph cross-link to syntheses.
