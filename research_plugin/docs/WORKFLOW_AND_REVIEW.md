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
3. Result sync gate: after execution, Codex must sync created/modified files as
   resources.
4. Results report gate: before `submit_results`, the current attempt must carry
   a short markdown report (role `report`) with Summary, Results (containing a
   metrics table: target vs achieved), Deviations from plan, and Conclusion
   applying the plan's pre-registered decision rule; under 10 KB; every
   relative figure link must resolve to a synced file. See
   `skills/research-workflow/report-template.md`.
5. Logic graph gate: before `submit_results`, the current attempt must also
   carry the experiment's logic graph (role `graph`) — the agent-authored
   story of notable decisions, problems, pivots, and lessons, told as a DAG.
   The server lints only the envelope (valid JSON, every node with an id and
   label, at most 16 nodes, acyclic edges, under 16 KB); the story's
   vocabulary, structure, and substance are the agent's design, judged by the
   experiment reviewer. See `skills/research-workflow/graph-template.md`.
6. Experiment review gate: a separate read-only experiment reviewer must pass
   the executed attempt, verifying the report against the raw result files and
   the logic graph's story against what actually happened.
7. Claim update gate: claim status/confidence changes require evidence links and
   a passing experiment or human review.

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

`return_to` is required on experiment-review rejections (`planned` or
`running`) and forbidden on `pass`; design-review rejections always go back to
`planned`.

## Codex responsibilities after a run

After any experiment execution, Codex should:

1. identify changed files with git/status or filesystem checks
2. decide which files are research resources
3. call `resource.register_file` with the changed `paths`
4. ask `workflow.status_and_next`
5. launch experiment reviewer if requested
6. wait for review submission status through MCP
7. if rejected: revise the plan when sent back to `planned`, or fix
   execution/conclusion and resubmit results when sent back to `running`
8. propose experiment conclusion or claim update only after passing review

Resources from prior attempts remain visible as experiment history, but MCP only
uses current-attempt resource associations to decide whether result sync or
review gates are satisfied.

While an experiment is `running` and no result resource is associated yet,
`workflow.status_and_next` returns `run_experiment_and_sync_results` with
`resource_guidance`. Agents run the experiment on the sandbox over SSH, then sync
the output files and associate them to the experiment with
`association_role: "result"`. Once results are synced but no report exists, the
gate becomes `results_report_required` with report-specific guidance; once a
report exists but no logic graph does, the gate becomes `logic_graph_required`.
The `submit_results` transition lints the report file (sections, metrics table,
size, figure links) and the logic graph's envelope (valid JSON, node budget,
DAG) before the experiment enters review.

On rejection, the attached revision context includes a soft reminder to
*consider* updating the logic graph — whether the rejection and rework belong
in the story is the agent's editorial call, and the 16-node budget still
applies.

## Completion rule

An experiment can complete only when:

- its result resources are synced
- its design and experiment review gates are satisfied
- its conclusion is tied to resources and/or sandbox outputs
- MCP accepts the completion transition
