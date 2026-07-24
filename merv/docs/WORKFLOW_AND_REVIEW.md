# Workflow And Review

## Experiment lifecycle

The experiment lifecycle is small and explicit about review gates:

```text
planned -> design_review -> ready_to_run -> running -> experiment_review -> complete
            ^             |                            ^                 |
            |             v                            |                 v
            +------ needs_changes              return_to=running   needs_changes / fail
            |                                          |                 |
            |                                          +--- (plan stands; fix
            |                                                execution/conclusion)
            +---------------- return_to=planned (plan is flawed) --------+

Rejections attach revision context either way. failed / abandoned are terminal exits.
```

An agent should not guess the state, the gate, or the next step. In a
project-linked MCP session it asks:

```text
workflow.status_and_next(experiment_id?)
```

The gateway injects the key's bound `project_id`; brain services and HTTP routes still
require explicit project scope. The agent should call `project(action="current")`
and link or create a project before any claim, experiment, artifact, review, or
sandbox workflow.

The server response is useful to both the agent and the user: current project
summary, experiment status, active attempt, linked claims, submitted plan and
result artifacts, review state, blocked actions, allowed actions, and next action.

## Required gates

Current gates:

1. Plan gate: before design review, the submitted plan must contain non-empty
   **Summary**, **Objective & hypothesis**, and **Evaluation** sections. The
   design reviewer judges the substantive method, outputs, decision rule,
   success criteria, risks, and confounders.
2. Design review gate: a separate read-only design reviewer must pass the plan.
3. Result retention gate: after execution, the agent must retain the selected
   outputs locally and submit them as artifacts.
4. Results report gate: before `submit_results`, the current attempt must carry
   a short markdown report (role `report`) with Summary, Results, Deviations
   from plan, and Conclusion sections; under 16 KB; every relative figure link
   must resolve to a submitted figure file. The linter checks the sections and
   shape; the reviewer judges whether the Conclusion actually applies the
   plan's pre-registered decision rule. At `submit_results`, the system pins a
   **metrics exhibit** when it finds attempt-window MLflow runs, or when MLflow
   is unavailable after a plugin-created run established quantitative intent.
   The exhibit contains up to the newest 50 matching runs plus eligible pinned
   result JSON, each entry with provenance. When an exhibit is pinned, the report must
   reference and interpret it — the server no longer polices agent-written
   metric tables; the exhibit is the record. An unconfigured/no-run fallback
   can have quantitative result files without producing an exhibit, so that
   reference gate is conditional. See
   `skills/research-workflow/report-template.md`.
5. Logic graph gate: before `submit_results`, the current attempt must also
   carry the experiment's logic graph (role `graph`) — a qualitative story
   the agent writes about the experiment's logical path: the hard decisions,
   the reasoning behind them, pivots, and lessons, told as a DAG. It is not
   an event or pipeline diagram and must be authored, never script-generated.
   The server lints only the envelope: valid JSON with `version: 1`; a non-empty
   node list with unique ids and non-empty labels; a list of edges whose
   endpoints exist, contain no self-loops, and form a DAG; at most 16 nodes; and
   a file under 16 KB. The story's vocabulary, structure, and substance are the
   agent's design, judged by the experiment reviewer. See
   `skills/research-workflow/graph-template.md`.
6. Experiment review gate: a separate read-only experiment reviewer must pass
   the executed attempt, verifying the report against the raw result files and
   the logic graph's story against what actually happened.
Direct `claim.update` validates status and confidence vocabulary but does not
enforce an evidence gate. Reviewed experiment completion returns advisory claim
update suggestions, while a published reflection can apply claim changes from
its reviewed change spec.

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
- expected output files are named
- risks and invalidating failure modes are explicit
- the plan is small enough to run and review

If design review fails, MCP returns the experiment to `planned` with review
feedback attached. The producer revises the plan and asks MCP for status again.

## Experiment reviewer contract

The experiment reviewer runs after execution, after selected outputs have been
pulled locally and registered/associated. It is read-only and submits its
review directly to MCP.

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
- include a `synopsis`: the researcher's 1-3 sentence TLDR, plain prose, no ids
- avoid mutating research state directly

A rejection routed to `running` keeps the approved plan and the current attempt
intact: the agent fixes execution and/or the conclusion, pulls any revised
outputs locally, registers/associates the new versions, and resubmits for
experiment review — no new design review. A rejection routed to
`planned` advances the attempt counter and requires a revised plan to pass a
fresh design review. Either way the experiment is never silently moved to
`failed` — that exit stays explicit. A rejection back to `planned` should carry
forward prior attempt context:

- previous plan
- result artifacts
- reviewer findings
- what can be reused
- what must be changed
- whether the issue was design, execution, metric, data, or conclusion

## Reviewer identity

Reviewer identity is modeled by MCP-issued capabilities.

1. The producer calls `review.request`.
2. MCP creates a review request and a request-scoped reviewer capability. Its
   plaintext value is returned only in that response; durable state stores a
   hash.
3. The producer spawns the proper reviewer agent with the role skill and capability.
4. Reviewer calls `review.start`, inspects only allowed context, and calls
   `review.submit`.
5. `review.request` verifies the workflow role against the active gate;
   `review.start` verifies the target snapshot, capability, and that the
   caller-supplied reviewer session string is non-empty and differs from the
   producer-supplied string. A passing submit satisfies the gate only while the
   request and snapshot remain current.

Step 3 is deliberate: `review.request` returns a
`reviewer_handoff.spawn_prompt` that already contains the skill name, request
id, and capability. Only the spawned reviewer should present it via
`review.start`, with a caller session distinct from the producer. Starting a
review does not consume the capability; the first accepted submission closes
the request.

`workflow.status_and_next` keeps the user-facing stage as `design_review` or
`experiment_review`, but exposes `workflow.review_gate.status` for the substate:

- `none`: no request exists; call `review.request`.
- `requested`: request exists, but no reviewer has started; launch the reviewer.
- `started`: reviewer is active; wait and poll `workflow.status_and_next`.
- `attested_blocked`: only a legacy attested pass exists while project policy
  requires a verified review; request a fresh review.

This is a practical session-separation check, not cryptographic proof of
independent reasoning. See [REVIEW_IDENTITY.md](REVIEW_IDENTITY.md) for the exact
checks and limitation.

The reviewer submits through MCP:

```text
review.submit(review_session_id, verdict, synopsis, return_to?, notes, findings, evidence?)
```

`return_to` is required on experiment-attempt-review rejections (`planned` or
`running`) and forbidden on `pass`; experiment-design-review rejections always go back to
`planned`.

`synopsis` is required on every submission: 1-3 plain sentences (40-420
chars), the researcher's TLDR and the first thing rendered on the experiment
page. It must read as plain prose in reader context — no entity ids
(`exp_`/`claim_`/`res_`/`rev_`/`rver_`/`syn_`), no backticks or markdown, no
newlines. `notes` and `findings` remain the machine-flavored detail; the
synopsis is the human-readable headline above them.

## Agent responsibilities after a run

After any experiment execution, the agent should:

1. identify changed files with git/status or filesystem checks
2. decide which retained files carry the mandated roles
3. submit each with `artifact.submit` (target, role, relative path) and run
   the returned upload command verbatim
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

Artifacts from prior attempts remain visible as experiment history, but MCP only
uses current-attempt artifacts to decide whether result retention or
review gates are satisfied.

While an experiment is `running` and no result artifact is submitted yet,
`workflow.status_and_next` returns `run_experiment_and_retain_results` with
`artifact_guidance`. Agents may execute locally or on a sandbox. After sandbox
execution they pull selected output files back to the checkout with
`sandbox.pull_outputs`, then submit the metrics JSON with `artifact.submit`
(`role: "result"`). Once
result artifacts exist but no report exists, the gate becomes
`results_report_required` with report-specific guidance; once a report exists
but no logic graph does, the gate becomes
`logic_graph_required`.
The `submit_results` transition first evaluates the system metrics exhibit
(preview it earlier with `experiment.exhibit`) and pins it when attempt-window
runs are present, or when MLflow is unavailable after a plugin-created run
established quantitative intent. Qualitative/no-run attempts receive no pinned
exhibit. It then lints the report file (sections, the exhibit reference when one
is pinned, size, figure links) and the logic graph's envelope (valid JSON, node
budget, DAG) before the experiment enters review.
Runs logged after `submit_results` remain in MLflow but are outside the already
finalized exhibit for that attempt.

If infrastructure fails while the experiment is already `running` and the
approved plan still stands, call
`experiment.transition(transition="retry_running", evidence={...})`. This is a
self-transition: the experiment remains `running`, `attempt_index` is unchanged,
and `revision_context` records the retry reason so the agent reruns execution
and retains fresh outputs before `submit_results`. Revise through the review
loop or create a new experiment instead when the plan itself needs to change.

`workflow.status_and_next` runs those same deep lints once every required
artifact exists: if a submitted artifact would fail the transition's lint, the gate is
`plan_invalid` / `report_invalid` / `graph_invalid` with the lint problems in
`missing_evidence` and the action `fix_<role>_artifact` — the workflow never
answers "ready to submit" for an artifact the transition would reject. The
lints read the SUBMITTED bytes (pinned at upload), so clearing
the gate means fixing the file AND resubmitting it.

On rejection, the attached revision context includes a soft reminder to
*consider* updating the logic graph — whether the rejection and rework belong
in the story is the agent's editorial call, and the 16-node budget still
applies.

## Completion rule

`complete` is accepted only from `experiment_review` when a passing current-
snapshot `experiment_reviewer` review satisfies the gate. The earlier
`submit_results` transition has already enforced the current attempt's result,
report, and graph artifacts. A conclusion is useful and drives claim-update
suggestions, but it is not an additional completion precondition.

## Project reflection lifecycle (reflection waves)

One level above experiments, the project maintains a **living project logic
graph** — one JSON file under the same 16-node envelope as experiment graphs,
holding the project's current logic state — through gated **reflection**
records (`syn_…` ids), one per reflection wave:

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
   roster lens has a current-attempt role-`reflection_lens_doc` artifact
   submitted with that explicit `lens_id` and non-empty pinned content. Each
   reflection is authored and submitted by its own subagent. The subagent reads
   project state without mutating it, then submits its own lens document as
   the one required mutation.
3. Reflection artifacts gate: `submit_reflection_artifacts` requires the project logic
   graph (role `project_graph`, the shared `graph_lint` envelope), a concise reflection
   document (role `reflection_doc`, required sections: Summary, Critical
   reading, Decision / future directions, under 16 KB, with any relative image
   links submitted alongside the markdown), and a materializable change spec
   (role `change_spec`), all associated to the wave's current attempt. The
   change spec is the reviewed belief-state update: claim creations/updates
   plus a `create_experiments` decision with 1-3 planned experiments (each
   carrying a `parallelism` note when the wave has more than one).
4. Reflection review gate: `publish` requires a passing `reflection_reviewer`
   review pinned to the wave's snapshot. The reviewer judges substance —
   does the story reconcile with the corpus, were the lenses genuinely
   diverse, is the belief-state update warranted, are the concrete experiments
   justified — through the same capability machinery (plaintext returned once,
   snapshot pinning, producer-session rejection, reviewer-skill boundary) as
   experiment reviews. Rejections must route via `return_to`: `synthesizing`
   or `reflecting`.

`reflection.get.gate_checklist` exposes the current gate as checklist data:
missing per-lens reflections in `reflecting`, missing/invalid graph-doc-spec
artifacts in `synthesizing`, and pending/requested/started/passed review state
in `reflection_review`. A legacy attested pass may surface as
`attested_blocked` when the project requires verified reviews.

On publish the record pins `published_graph_version_id`, so the single living
graph file still yields an immutable per-wave history. The same publish
transaction applies approved claim changes and creates the approved planned
experiments. Stopping remains a researcher decision and is not a valid change-
spec action. Rejected reflection waves do not
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
  than answering "none" for the auto-resolved terminal experiment. When the
  project is *not* idle but the auto-resolved (newest-created) experiment is
  terminal, the workflow block instead lists the live siblings
  (`current_gate: live_experiments`) so the agent re-orients onto in-flight
  work or creates the next experiment.
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
