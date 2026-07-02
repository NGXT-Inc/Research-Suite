# Workflow And Review

## Experiment lifecycle

The server should keep the experiment lifecycle small but explicit about review
gates:

```text
idea -> planned -> design_review -> ready_to_run -> running -> experiment_review -> complete
            ^             |                            ^                 |
            |             v                            |                 v
            +------ needs_changes              return_to=running   needs_changes / fail
            |                                          |                 |
            |                                          +--- (plan stands; fix
            |                                                execution/conclusion)
            +---------------- return_to=planned (plan is flawed) --------+

Rejections attach revision context either way. failed / abandoned are terminal exits.
```

Codex should not guess the state, the gate, or the next step. It should ask:

```text
workflow.status_and_next(project_id, experiment_id?)
```

`project_id` is required. Codex should select or create a project before any
claim, experiment, resource, review, or sandbox workflow.

The server response should be useful to both Codex and the user: current project
summary, experiment status, active attempt, linked claims, plan resources, result
resources, review state, blocked actions, allowed actions, and next action.

## Required gates

MVP gates:

1. Plan gate: before expensive execution, the plan must identify claim, method,
   inputs, outputs, metric, and expected resource files.
2. Design review gate: a separate read-only design reviewer must pass the plan.
3. Result retention gate: after execution, Codex must retain the selected
   outputs locally and register/associate them as resources.
4. Results report gate: before `submit_results`, the current attempt must carry
   a short markdown report (role `report`) with Summary, Results (containing a
   metrics table: target vs achieved), Deviations from plan, and Conclusion
   applying the plan's pre-registered decision rule; under 16 KB; every
   relative figure link must resolve to a submitted figure file. See
   `skills/research-workflow/report-template.md`.
5. Logic graph gate: before `submit_results`, the current attempt must also
   carry the experiment's logic graph (role `graph`) — a qualitative story
   the agent writes about the experiment's logical path: the hard decisions,
   the reasoning behind them, pivots, and lessons, told as a DAG. It is not
   an event or pipeline diagram and must be authored, never script-generated.
   The server lints only the envelope (valid JSON, every node with an id and
   label, at most 16 nodes, acyclic edges, under 16 KB); the story's
   vocabulary, structure, and substance are the agent's design, judged by the
   experiment reviewer. See `skills/research-workflow/graph-template.md`.
6. Experiment review gate: a separate read-only experiment reviewer must pass
   the executed attempt, verifying the report against the raw result files and
   the logic graph's story against what actually happened.
7. Claim update gate: claim status/confidence changes require evidence links and
   a passing experiment or human review.

## Active experiment cap

A project may have at most 7 active experiments at once. Active means any
experiment whose status is not terminal (`complete`, `failed`, or `abandoned`).
Direct `experiment.create` is blocked at the cap with:

```text
active experiment cap reached: project has 7 active experiments; finish one before creating another.
```

Reflection-published change specs must also fit available active slots. The
change spec is rejected before review, and publish rechecks defensively, if the
proposed experiment wave would exceed the cap.

## Design reviewer contract

The design reviewer runs before expensive execution. It is read-only and submits
its review directly to MCP.

The design reviewer checks:

- the claim and scope are clear
- the experiment can actually test the linked claim
- inputs, outputs, metric, baseline, and success criteria are defined
- expected resource files are named
- risks and invalidating failure modes are explicit
- the plan is small enough to run and review

If design review fails, MCP returns the experiment to `planned` with review
feedback attached. Codex revises the plan and asks MCP for status again.

## Experiment reviewer contract

The experiment reviewer runs after execution and resource sync. It is read-only
and submits its review directly to MCP.

The experiment reviewer checks:

- inspect the experiment plan
- inspect relevant code and result files
- check whether the experiment actually tests the linked claim
- look for leakage, metric misuse, missing baseline, cherry-picked results, and
  unsupported conclusions
- return `pass`, `fail`, or `needs_changes`
- on rejection, choose `return_to`: `planned` when the results revealed a flaw
  in the plan itself; `running` when the plan stands but execution or the
  conclusion is flawed
- include concrete findings
- avoid mutating research state directly

A rejection routed to `running` keeps the approved plan and the current attempt
intact: the agent fixes execution and/or the conclusion, re-syncs results, and
resubmits for experiment review — no new design review. A rejection routed to
`planned` advances the attempt counter and requires a revised plan to pass a
fresh design review. Either way the experiment is never silently moved to
`failed` — that exit stays explicit. A rejection back to `planned` should carry
forward prior attempt context:

- previous plan
- result resources
- reviewer findings
- what can be reused
- what must be changed
- whether the issue was design, execution, metric, data, or conclusion

## Reviewer identity

Local reviewer identity should be modeled by MCP-issued capabilities.

1. Main Codex calls `review.request`.
2. MCP creates a review request and one-time read-only reviewer capability.
3. Main Codex spawns the proper reviewer agent with the role skill and capability.
4. Reviewer calls `review.start`, inspects only allowed context, and calls
   `review.submit`.
5. MCP verifies role, target snapshot, capability, and session lineage before
   satisfying the gate.

Step 3 is deliberate and cannot be skipped: `review.request` returns a
`reviewer_handoff.spawn_prompt` that already contains the skill name, request
id, and one-time capability, but only the spawned reviewer consumes it via
`review.start`. A helper that started the session server-side was removed —
it let the requesting session submit against its own gate.

`workflow.status_and_next` keeps the user-facing stage as `design_review` or
`experiment_review`, but exposes `workflow.review_gate.status` for the substate:

- `none`: no request exists; call `review.request`.
- `requested`: request exists, but no reviewer has started; launch the reviewer.
- `started`: reviewer is active; wait and poll `review.status`.

This is a practical local independence check, not cryptographic proof. For
high-risk work, MCP can require human review.

The reviewer submits through MCP:

```text
review.submit(review_session_id, verdict, return_to?, notes, findings, evidence?)
```

`return_to` is required on experiment-attempt-review rejections (`planned` or
`running`) and forbidden on `pass`; experiment-design-review rejections always go back to
`planned`.

## Codex responsibilities after a run

After any experiment execution, Codex should:

1. identify changed files with git/status or filesystem checks
2. decide which files are research resources
3. call `resource.register_file` with the retained local `paths`
4. ask `workflow.status_and_next`
5. launch experiment reviewer if requested
6. wait for review submission status through MCP
7. if rejected: revise the plan when sent back to `planned`, or fix
   execution/conclusion and resubmit results when sent back to `running`
8. propose experiment conclusion or claim update only after passing review

Once a reviewed experiment is completed, `experiment.get_state` includes
`claim_update_suggestions` for every tested claim when a conclusion is present.
These are pre-scoped `claim.update` call skeletons; they are suggestions, not
automatic mutations.

Resources from prior attempts remain visible as experiment history, but MCP only
uses current-attempt resource associations to decide whether result retention or
review gates are satisfied.

While an experiment is `running` and no result resource is associated yet,
`workflow.status_and_next` returns `run_experiment_and_retain_results` with
`resource_guidance`. Agents run the experiment on the sandbox over SSH, then
pull selected output files back to the local checkout with `sandbox.pull_outputs`
and associate them to the experiment with `association_role: "result"`. Once
result resources exist but no report exists, the gate becomes
`results_report_required` with report-specific guidance; once a report exists
but no logic graph does, the gate becomes
`logic_graph_required`.
The `submit_results` transition lints the report file (sections, metrics table,
size, figure links) and the logic graph's envelope (valid JSON, node budget,
DAG) before the experiment enters review.

If infrastructure fails while the experiment is already `running` and the
approved plan still stands, call
`experiment.transition(transition="retry_running", evidence={...})`. This is a
self-transition: the experiment remains `running`, `attempt_index` is unchanged,
and `revision_context` records the retry reason so the agent reruns execution
and retains fresh outputs before `submit_results`. Use `return_to="planned"` or
a new experiment instead when the plan itself needs to change.

`workflow.status_and_next` runs those same deep lints once every required
artifact exists: if a submitted artifact would fail the transition's lint, the gate is
`plan_invalid` / `report_invalid` / `graph_invalid` with the lint problems in
`missing_evidence` and the action `fix_<role>_resource` — the workflow never
answers "ready to submit" for an artifact the transition would reject. The
lints read the SUBMITTED bytes (pinned at resource.associate), so clearing
the gate means fixing the file AND re-associating it to submit the revision.

On rejection, the attached revision context includes a soft reminder to
*consider* updating the logic graph — whether the rejection and rework belong
in the story is the agent's editorial call, and the 16-node budget still
applies.

## Completion rule

An experiment can complete only when:

- its result resources are retained and associated
- its design and experiment review gates are satisfied
- its conclusion is tied to resources and/or sandbox outputs
- MCP accepts the completion transition

## Project synthesis lifecycle (reflection waves)

One level above experiments, the project maintains a **living project logic
graph** — one JSON file under the same 16-node envelope as experiment graphs,
holding the project's current logic state — through gated **synthesis**
records (`syn_…`), one per reflection wave:

```text
reflecting -> synthesizing -> reflection_review -> published
    ^               ^                |
    |               |                v
    |               +--- return_to=synthesizing (reflections stand;
    |                     revise graph/doc/spec; attempt unchanged)
    +------------------- return_to=reflecting (re-launch the fan-out;
                          attempt advances)

abandoned is the terminal exit. One wave may be open per project at a time.
```

Gates (envelope-only, same philosophy as experiment gates):

1. Roster gate: `reflection.create` requires exactly five lenses — the three
   core ids (`amplify`, `avoid`, `entropy`) plus two wave-authored
   lenses, each with a charter and a stated `why_distinct`. The corpus
   (terminal experiments + claim statuses) is snapshotted at create.
2. Reflection coverage gate: `submit_reflections` is blocked until every
   roster lens has a current-attempt role-`reflection_lens_doc` resource whose
   filename is `<lens_id>.md`, non-empty on disk. Each reflection is
   authored and submitted by its own read-only subagent.
3. Synthesis artifacts gate: `submit_reflection_artifacts` requires the project logic
   graph (role `project_graph`, the shared `graph_lint` envelope), a concise reflection
   document (role `reflection_doc`, required sections: Summary, Critical
   reading, Decision / future directions, under 16 KB, with any relative image
   links submitted alongside the markdown), and a materializable change spec
   (role `change_spec`), all associated to the wave's current attempt. The
   change spec is the reviewed belief-state update: claim creations/updates
   plus exactly one decision — `hard_stop` or `create_experiments` with 2-3
   planned experiments.
4. Synthesis review gate: `publish` requires a passing `reflection_reviewer`
   review pinned to the wave's snapshot. The reviewer judges substance —
   does the story reconcile with the corpus, were the lenses genuinely
   diverse, is the belief-state update warranted, are the concrete experiments
   or hard stop justified — through the same capability machinery (one-time
   token, snapshot pinning, producer-session rejection, read-only funnel) as
   experiment reviews. Rejections must route via `return_to`: `synthesizing`
   or `reflecting`.

`reflection.get.gate_checklist` exposes the current gate as checklist data:
missing per-lens reflections in `reflecting`, missing/invalid graph-doc-spec
artifacts in `synthesizing`, and pending/requested/started/passed review state
in `reflection_review`.

On publish the record pins `published_graph_version_id`, so the single living
graph file still yields an immutable per-wave history. The same publish
transaction applies approved claim changes and either marks the project
stopped or creates the approved planned experiments. Rejected reflection waves do not
mutate claims or create experiments. The diversity heuristics (anti-overlap
lens briefs, ambition quota, dead-end differentiation) live in the
`project-reflection` skill, not in gates.

Staleness is computed on read, never stored: `workflow.status_and_next`
carries a `project_reflection` block when a wave is open (slim state + gate
guidance) or when the project has drifted from the last published reflection.
Drift surfaces in three tiers:

- **Nudge** (any time): once drift crosses the staleness threshold (≥3
  newly-terminal experiments, or any claim flipped to `contradicted`), the
  block carries the soft hint — "Consider running a project reflection…" —
  whatever else is in flight.
- **Recommendation** (idle only): when no experiment is active and at least
  one has finished since the last published reflection, a project-level call
  (no explicit `experiment_id`) also escalates the workflow block itself:
  `current_gate: reflection_suggested`, `next_action:
  consider_project_reflection`, with `reflection.create` alongside
  `claim.create` / `experiment.create` in the allowed actions. An open wave
  wins that slot instead (its gate guidance becomes the workflow block), so
  an idle orientation call always points at the project-level work rather
  than answering "none" for the auto-resolved terminal experiment.
- **Required reflection**: once five newly-terminal experiments have accumulated
  since the last published reflection, `experiment.create` becomes a hard
  blocker. The workflow reports `current_gate: reflection_required`, removes
  `experiment.create` from allowed actions, and lists it in `blocked_actions`.
  The `experiment.create` tool itself rejects until a project reflection is
  published. Publishing a reflection applies the reviewed change spec, which can
  create the next planned experiment wave.

Explicitly experiment-scoped calls are never taken over; they get the side
block only. Claim creation can remain allowed even when experiment creation is
blocked.
