# MCP Server Contract

## Role

The MCP server is the authority for research state and workflow state. Codex can
reason and edit files, but MCP decides whether a state mutation is allowed, what
gate is active, and what the workflow requires next.

Codex should usually begin with the broad orientation tool:

```text
workflow.status_and_next(project_id, experiment_id?)
```

This tool exists because Codex may lose conversation memory. The server must be
able to re-orient the agent and the user from durable state.

## Implementation note

The MCP tool surface is owned by the long-running HTTP daemon (`bin/research-plugin-http`).
What Codex launches via the plugin manifest (`bin/research-plugin-mcp`) is a
thin stdio proxy that forwards `tools/list` and `tools/call` to the daemon's
`/mcp/tools` and `/mcp/call` endpoints. The proxy holds no state of its own —
everything in this contract is enforced inside the daemon. Both the browser
UI and the proxy go through the same `ResearchPluginApp.call_tool` path, so
permission checks and workflow gates are identical regardless of caller.

## Tool groups

### Memory tools

```text
project.status_and_next(project_id)
project.create(name, summary?)
project.update(project_id, name?, summary?)
project.get(project_id)
project.visible_summary(project_id)
claim.list(project_id)
claim.create(project_id, statement, scope?)
claim.propose_update(project_id, claim_id, patch, rationale)
experiment.list(project_id)
experiment.create(project_id, intent, tested_claim_ids?)
experiment.get(project_id, experiment_id)
experiment.get_state(project_id, experiment_id)
resource.list(project_id)
review.status(project_id, target_type, target_id)
event.list(project_id, limit?)
```

`experiment.create` is intentionally simple in durable storage: it creates a
planned experiment with one `intent` string and optional linked claims. The MCP
schema advertises `intent` and `tested_claim_ids` as the preferred shape, but
the server accepts common Codex/user aliases:

```text
claim_id -> tested_claim_ids[0]
claim_ids -> tested_claim_ids
title, hypothesis, design, success_criteria, risks -> folded into intent
status must be omitted or "planned"
```

Use `experiment.transition` for workflow state changes after creation.

### Resource tools

```text
resource.register_file(project_id, path, kind, title?)
resource.observe_file(project_id, path)
resource.sync_changed_files(project_id, paths?)
resource.resolve(project_id, resource_id)
resource.history(project_id, resource_id)
```

The server observes local repo files by path, stores latest metadata in
`resources`, and records append-only observations in `resource_versions`.
Each observed version captures size, mtime, content sha256, and mimetype;
file content itself is not stored — historical content lives in the user's
own repo / git history.

When a resource is associated with an experiment, MCP stores the experiment's
current `attempt_index` and current `version_id` on that association. Workflow
gates only count resources from the current attempt, so stale result files from
a failed attempt cannot satisfy a rerun.

`workflow.status_and_next` reconciles already-associated current-attempt
experiment resources before answering. If an associated live file changed, MCP
snapshots the new version and updates the association; if the file is missing,
MCP marks the resource missing. It does not scan the repo or register new files.

### Workflow tools

```text
workflow.status_and_next(project_id, experiment_id?)
workflow.next_action(project_id, experiment_id)
workflow.transition(project_id, experiment_id, transition, evidence?)
workflow.record_blocker(project_id, experiment_id, reason)
workflow.request_human_review(project_id, experiment_id, reason)
```

The server may reject transitions that skip required gates. `status_and_next`
should return enough visibility for both Codex and the user:

```json
{
  "project": {
    "id": "proj_...",
    "summary": "Current research objective.",
    "active_claims": [],
    "active_experiments": []
  },
  "experiment": {
    "id": "exp_...",
    "status": "experiment_review",
    "attempt": 2,
    "tested_claims": [],
    "plan_resources": [],
    "result_resources": [],
    "latest_reviews": []
  },
  "workflow": {
    "current_gate": "experiment_review",
    "next_action": "launch_experiment_reviewer",
    "allowed_actions": ["review.request"],
    "blocked_actions": [
      {
        "action": "experiment.complete",
        "reason": "missing passing experiment review"
      }
    ],
    "missing_evidence": [],
    "revision_context": "optional feedback from prior failed run"
  }
}
```

### Execution tools

```text
job.submit(project_id, experiment_id, command, cwd?, expected_outputs?, backend_hints?)
job.status(project_id, job_id)
job.logs(project_id, job_id, tail?)
job.cancel(project_id, job_id)
job.list(project_id, experiment_id?, status?)
job.health()
```

Codex can still run lightweight local commands itself. Use MCP jobs for
expensive, long-running, resumable, or policy-sensitive ML runs.

The execution backend is not exposed directly to Codex. Codex calls MCP job
tools; MCP validates the request, records local job state, and delegates to the
configured execution backend.

`workflow.status_and_next` may return last-known job summaries, but it must stay
a high-level orientation endpoint. Fresh backend polling belongs in
`job.status`; log retrieval belongs in `job.logs`; output availability is
reported in `job.status.outputs`.

The default backend is `modal`. Backend selection is controlled by
`RESEARCH_PLUGIN_EXECUTION_BACKEND`; supported values are currently `modal`,
`ray`, and `fake` for tests. Backend-specific configuration is supplied through
environment variables and optional opaque `backend_hints` on `job.submit`.

The Modal backend mirrors each project into a per-project Modal Volume, mounts
that volume writable at the remote workdir, runs jobs inside the mounted repo,
and syncs changes back to the local repo through the Modal sync engine.
`expected_outputs` is a workflow hint for output availability and result
resource association, not a per-file transfer instruction.

The Ray backend remains available for local development. It supports SDK mode,
when the MCP Python process has Ray installed, and REST mode otherwise. The
execution contract remains backend-neutral so additional providers can live
inside `execution/backends/`.

All project-scoped tools require an explicit `project_id`; the server does not
fall back to the first-created project. The UI and skills must select a project
first and pass that id through every scoped call.

### Review tools

```text
review.require(project_id, target_type, target_id, reason)
review.request(project_id, target_type, target_id, role, reason)
review.start(review_request_id, reviewer_capability, declared_agent?)
review.submit(review_session_id, verdict, notes, findings, evidence?)
review.status(project_id, target_type, target_id)
```

Reviewer roles:

- `design_reviewer`: reviews experiment plan before execution.
- `experiment_reviewer`: reviews executed attempt, result resources, metrics,
  and conclusion before completion or claim update.
- `human`: records a human decision with the same mechanism.
- `automated_check`: records deterministic checks or audit scripts.

Reviewers are read-only. A reviewer capability may only call read tools for the
target context plus `review.submit` for its own review request. Recording a
review does not automatically accept the underlying mutation unless policy says
the review satisfies the gate.

### Reviewer identity tools

```text
review.request(project_id, target_type, target_id, role, reason)
review.start(review_request_id, reviewer_capability, declared_agent)
review.submit(review_session_id, verdict, notes, findings, evidence?)
```

`review.request` returns:

```json
{
  "review_request_id": "rr_...",
  "reviewer_capability": "one-time-secret-or-handle",
  "role": "experiment_reviewer",
  "target_snapshot_id": "snap_...",
  "target_snapshot": {
    "target_type": "experiment",
    "target_id": "exp_...",
    "status": "experiment_review",
    "attempt_index": 2,
    "resources": [
      {"resource_id": "res_...", "version_id": "rver_...", "role": "result", "attempt_index": 2}
    ]
  },
  "expires_at": "2026-05-17T15:00:00Z"
}
```

`review.status` and the HTTP review queue expose the same `target_snapshot`
shape on review request and submitted review records. Frontends should use
`target_snapshot.resources[].version_id` for exact reviewed resource versions and
treat `target_snapshot_id` as an opaque backend fingerprint.

MCP should reject a review when:

- the capability is expired or reused
- the role does not match the active gate
- the target snapshot changed after the capability was issued
- the review session matches the producer session for the plan/result
- the reviewer attempts any mutation except `review.submit`

This creates a practical local independence boundary. It does not prove
cryptographic independence, so high-risk gates can require `human` review.

### Mutation tools

```text
state.propose_mutation(kind, payload, rationale)
state.apply_approved(change_id)
state.reject(change_id, reason)
```

Most MVP tools can be narrower than this, but the server should keep the same
mental model: proposed mutation, policy decision, accepted event.

## Permission rules

Start with simple policy, not full RBAC:

- read operations are allowed
- file observation is allowed only under repo root
- resource registration only accepts repo-relative paths
- experiment creation is allowed
- moving an experiment to `complete` requires synced result resources
- claim status changes require evidence and review
- destructive changes require explicit human approval
- reviewer agents cannot mutate state except by `review.submit`
- Codex cannot mark its own review as design or experiment review
- design review must pass before expensive execution
- experiment review must pass before completion or claim update
- a failed design review returns the experiment to `planned`
- a failed experiment review returns the experiment to `planned` with prior
  attempt context and review feedback preserved

## Response shape

Every mutating tool should return:

```json
{
  "ok": true,
  "accepted": true,
  "change_id": "optional",
  "requires_review": false,
  "next_action": "optional-machine-action",
  "message": "Human-readable status."
}
```

Every rejection should explain the blocked invariant:

```json
{
  "ok": false,
  "accepted": false,
  "error_code": "missing_experiment_review",
  "message": "Experiment cannot complete until a separate experiment reviewer submits a passing review.",
  "next_action": "launch_experiment_reviewer"
}
```

## Minimal persistence

Use one local SQLite database under `.research_plugin/state.sqlite`.

Tables can be minimal:

- claims
- experiments
- resources
- reviews
- review_requests
- review_sessions
- events
- jobs

No resource version table is needed for v0.1. Store the last observed file token
directly on the resource row and append a lightweight event when it changes.
