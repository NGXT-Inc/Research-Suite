# MCP Server Contract

## Role

The MCP server is the authority for research state and workflow state. Codex can
reason and edit files, but MCP decides whether a state mutation is allowed, what
gate is active, and what the workflow requires next.

Codex should usually begin with the broad orientation tool. In project-local MCP
sessions, the stdio proxy supplies `project_id` from hidden repo context, so the
agent-facing schema may omit it:

```text
workflow.status_and_next(experiment_id?)
```

This tool exists because Codex may lose conversation memory. The server must be
able to re-orient the agent and the user from durable state.

## Implementation note

The MCP tool surface is owned by the long-running HTTP daemon (`bin/research-plugin-http`).
What Codex launches via the plugin manifest (`bin/research-plugin-mcp`) is a
thin stdio proxy that forwards `tools/list` and `tools/call` to the daemon's
`/mcp/tools` and `/mcp/call` endpoints. The proxy holds no state of its own —
everything in this contract is enforced inside the daemon. The proxy does add
the current repo root as hidden context and hides `project_id` from
project-scoped tool schemas when that context can supply it. HTTP and core
service calls still carry explicit `project_id`.

In shared-daemon mode, `project.current` through MCP is folder-scoped and
returns the project registered for the folder where the MCP proxy was started,
or `exists: false` if that folder does not have a project yet. It never lists
projects from other folders and does not create a project as a side effect. If
`exists` is false, the agent should ask the user what project name and summary
to use before calling `project.create`, unless the user already supplied that
information.

## Tool groups

### Memory tools

```text
project.current()
workflow.status_and_next(project_id, experiment_id?)
project.create(name, summary?)
project.update(project_id, name?, summary?)
project.get(project_id)
claim.list(project_id)
claim.create(project_id, statement, scope?)
claim.propose_update(project_id, claim_id, patch, rationale)
experiment.list(project_id)
experiment.create(project_id, intent, tested_claim_ids?)
experiment.get(project_id, experiment_id)
experiment.get_state(project_id, experiment_id)   # see "get_state shape" below
resource.list(project_id, kind?, experiment_id?, missing?, compact?, limit?, offset?)
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
title, hypothesis, design, success_criteria, risks -> deprecated; no longer
  folded into intent. `intent` is the one-line headline; the full design lives
  in the plan.md resource. These aliases are accepted for back-compat and, only
  when `intent` is empty, the first non-empty one becomes the headline.
status must be omitted or "planned"
```

Use `experiment.transition` for workflow state changes after creation.

The plan resource follows a PRD-style schema (see
`skills/research-workflow/plan-template.md`). `experiment.transition(submit_design)`
is gated on a required spine — **Summary**, **Objective & hypothesis**,
**Evaluation** — each present and non-empty in the plan file; the design
reviewer judges whether the recommended sections (Method, Outputs, Risks) are
sufficient.

### Resource tools

```text
resource.register_file(project_id, path?, paths?, kind, title?)  # single file or batch
resource.resolve(project_id, resource_id, include_history?)       # include_history adds versions
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

The server may reject transitions that skip required gates. The agent-facing
tool returns a **slim** projection — only what the next-action decision and the
agent need — because this call is polled constantly and the underlying state is
large. (The UI gets the full shape via the HTTP `/status` endpoints, which call
the service directly.) The slim shape, scoped to an experiment:

```json
{
  "scope": "experiment",
  "workflow": {
    "current_gate": "experiment_review",
    "next_action": "launch_experiment_reviewer",
    "allowed_actions": ["review.request"],
    "blocked_actions": [
      { "action": "experiment.complete", "reason": "missing passing experiment review" }
    ],
    "missing_evidence": [],
    "revision_context": ""
  },
  "experiment": {
    "id": "exp_...",
    "status": "experiment_review",
    "attempt_index": 2,
    "intent": "…",
    "conclusion": "",
    "updated_at": "2026-06-03T04:41:37Z",
    "tested_claim_ids": ["claim_..."],
    "current_attempt_resources": [
      { "id": "res_...", "association_role": "result",
        "path": "experiments/004/results/status.json", "kind": "other",
        "missing": 0, "size_bytes": 341 }
    ],
    "reviews": [
      { "id": "rev_...", "role": "design_reviewer", "verdict": "pass", "created_at": "…" }
    ]
  },
  "sandbox": {
    "active": false,
    "last_status": "terminated",
    "note": "No active sandbox for this experiment — call sandbox.request to create or reuse one."
  },
  "project": { "id": "proj_...", "name": "…" }
}
```

When a sandbox is live, `sandbox` is `{ "active": true, "sandbox_id", "status",
"gpu", "cpu", "memory", "ssh_host", "ssh_port", "ssh_user", "workdir",
"sync_dir", "unsynced_dir", "local_sync_dir", "sandbox_data_dir", "expires_at" }`.
Dropped vs. the underlying `experiment.get_state`: the duplicate
all-attempts `resources` list, per-resource version bookkeeping (`version_token`,
`mtime_ns`, `*_version_id`, `git_commit`, timestamps), full review
prose/`evidence`/`target_snapshot_id`, and the project-wide claim/experiment
detail. For those, call the scoped tools (`experiment.get_state`,
`resource.list`, `review.status`). Called **without** `experiment_id` (only at
project setup, before any experiment exists), it returns
`{ "scope": "project", "workflow", "project": { id, name, summary, claims[] } }`.

#### get_state shape

`experiment.get_state` (and the per-experiment entries of `experiment.list`) is
the *detail* call, so it keeps the substance — `intent`, `conclusion`, the
resource list, and full review `findings` / `notes` / `evidence` / `verdict`.
It also carries `allowed_transitions`: the transitions available from the
current status, each with `leads_to` and (where gated) a `requires` hint — so
the agent learns the next legal step and its preconditions without trial and
error. What it drops is pure waste:

- the duplicate all-attempts `resources` list (a copy of
  `current_attempt_resources`); resources from *earlier* attempts appear instead
  as a compact `prior_attempt_resources: [{id, association_role, path,
  association_attempt_index}]`, present only when a rerun produced them;
- per-resource bookkeeping — `version_token` (itself `path:mtime:mtime:size`),
  `mtime_ns`, the two usually-equal `*_version_id`, the three timestamps,
  repeated `project_id`, constant `created_by`/`git_commit`/
  `association_attempt_index`. Each resource keeps `{id, association_role, path,
  kind, size_bytes, missing, title}`;
- review internals — `target_snapshot_id`, `request_id`, `session_id`,
  `target_id`, `target_type`, `project_id`.

The UI gets the full shape (the HTTP routes call the service directly). For
per-resource version history, use `resource.resolve(include_history=true)`.

### Execution tools

```text
sandbox.options(gpu?, region?)
sandbox.request(experiment_id, instance_type?, region?, gpu?, cpu?, memory?, time_limit?)
sandbox.get(experiment_id)
sandbox.sync(experiment_id)
sandbox.list()
sandbox.release(experiment_id)
sandbox.terminal(experiment_id, tail?, since?)   # cursor + running; poll with since=cursor for new output. Also last_exit_code / last_command_finished_at / command_running per command.
sandbox.health()
```

There is no job abstraction. Codex requests a sandbox for an experiment, gets
back SSH connection details (including a short, ready-to-run `ssh.command`), and
runs shell commands on the sandbox itself. Lightweight work still runs locally.

`sandbox.request` is the only procurement call. The registry keeps **one sandbox
per experiment** and reuses the live one if it is still alive, otherwise creates
a fresh one. The response carries `ssh` (host, port, user, key_path, command,
raw_command), `workdir`, `sync_dir`, `unsynced_dir`, `local_sync_dir`,
`sandbox_data_dir`, `status`, `expires_at`, `reused`, and — when set — the
reserved hardware (`gpu`, `cpu`, `memory`, `instance_type`, `region`).
`ssh.command` is the short dispatcher form
`.research_plugin/sbx <experiment_id>` (run from the repo root); `ssh.raw_command`
is the full `ssh -i … user@host` line for use from any directory.

#### Hardware selection (provider-shaped)

Procurement differs by backend, and the **default backend is Lambda Labs**:

- **Lambda Labs (default)** sells fixed machine SKUs that bundle GPU + vCPU + RAM
  together, so the agent picks an `instance_type` rather than independent
  cpu/memory. When `sandbox.request` arrives with **no `instance_type`** and the
  experiment has **no live sandbox to reuse**, the server does **not** provision.
  It returns `status: "needs_selection"` with a live, cheapest-first `options`
  menu (each entry: `instance_type`, `gpu`, `gpu_count`, `vcpus`, `memory_gib`,
  `storage_gib`, `price_usd_per_hour`, `regions`). The agent re-calls
  `sandbox.request(experiment_id, instance_type=<choice>, region?=<choice>)`.
  Omit `region` to auto-pick a region that currently has capacity. On Lambda,
  `gpu` is a free-form *filter* over the menu and `cpu`/`memory` are ignored (the
  SKU fixes them).
- **Modal** composes the machine from the request: set `gpu` (a concrete
  attachable GPU, e.g. `A100`/`H100`; omit for CPU-only), `cpu` (Modal CPU cores,
  1 core = 2 vCPUs), and `memory` (MiB). Modal never returns `needs_selection`.

`sandbox.options` is the read-only discovery call: it returns the active
backend's current catalog (Lambda: live available instance types; Modal: the
gpu/cpu/memory menu) plus a `hint` on how to request. It never provisions.

Agents should prefer CPU-only / smaller machines for exploratory data
inspection, dataset downloads, schema checks, preprocessing scripts, joins,
filtering, and other data engineering unless a command specifically needs GPU
acceleration. On Lambda that means picking the smallest/cheapest viable SKU from
the menu; on Modal, omitting `gpu`.

`workdir` is the synced working directory, currently the same as `sync_dir`
(default `/workspace/synced`). `unsynced_dir` / `sandbox_data_dir` is
sandbox-local ephemeral storage outside the synced tree (default
`/workspace/unsynced`) and is exported inside SSH commands as
`$RP_UNSYNCED_DIR`, `$RP_SANDBOX_DATA_DIR`, and `$RP_DATASET_DIR`. Agents should
download large datasets, caches, checkpoints, parquet files, and heavy
intermediates there, then write only scripts, configs, metrics, and compact
results under `sync_dir`. Data transformations and temporary derived datasets
left under `unsynced_dir` are ephemeral and will not carry into future
sandboxes. If a large artifact deliberately must be preserved locally, agents
place it under `$RP_SYNC_DIR/artifacts_to_keep`; this subdirectory is pulled by
a separate higher-size rsync pass. Agents should also prefer to save a Markdown
data note in the experiment folder under `sync_dir` (for example
`experiments/<name>/data.md`) describing datasets used, source identifiers,
split/filter choices, important columns, row counts, caveats, and where large
ephemeral files were placed in `unsynced_dir`.

When the backend has `HF_TOKEN` in its env file or process environment,
`sandbox.request` / `sandbox.get` include an `environment.available_tokens`
entry naming `HF_TOKEN`. The token value is not returned. Inside SSH commands,
`HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN` are available for Hugging Face tooling.
The backend passes the token through Modal's sandbox `secrets` API, not as a
plain sandbox `env` value and not as a synced repo `.env` file.
Agents must not print the token, write it into synced files, or register it as a
resource.

When a fresh sandbox/VM is created, setup pushes the experiment's `local_sync_dir`
to `$RP_SYNC_DIR` before returning `status: running`. This makes existing local
experiment files available to a newly provisioned remote environment. Before
registering or associating result resources, the agent must call
`sandbox.sync(experiment_id)`: the daemon pulls `$RP_SYNC_DIR` over SSH with
`rsync` into the experiment's `local_sync_dir`. The regular pass skips heavy file
types and enforces a conservative file-size limit; `$RP_SYNC_DIR/artifacts_to_keep`
is the deliberate exception path for large final artifacts. Resource tools only
operate on local repo files, so a file produced remotely cannot be associated
until it has synced locally. Call `sandbox.sync` before `sandbox.release` for
any outputs that must survive, and after major sandbox file changes so the user
can inspect the latest local files while the sandbox is still running. The
daemon also runs a best-effort periodic rsync for live sandboxes.

Provisioning is **best-effort-synchronous**. Creating a sandbox can outlast the
MCP call timeout (large first sync, cold GPU), so `sandbox.request` provisions on
a background thread and waits up to a budget (default 45s,
`RESEARCH_PLUGIN_SANDBOX_REQUEST_WAIT`):

- settles in time → `status: "running"` with `ssh.command`, exactly as before;
- still working → `status: "provisioning"` with `phase`, `detail`, and
  `poll_after_seconds`, and no `ssh.command` yet.

`sandbox.get` is the **poll**: read-only, never provisions, and returns the
current row — `provisioning` (keep polling), `running` (use `ssh.command`),
`failed` (read `error`, then `sandbox.request` to retry), `terminated`, or
`none` (never requested; not an error). It reconciles a `provisioning` row whose
background job died (daemon restart) to `failed` so a poll loop always reaches a
terminal state. `sandbox.release` also cancels an in-flight provision. The agent
contract: call `request`, then if `provisioning` poll `get` every
`poll_after_seconds` until `running`/`failed` — never re-call `request` to poll.

Visibility: every SSH command and its output are recorded to a per-experiment
transcript inside the sandbox. `sandbox.terminal` reads it live from the sandbox.
The UI renders it as a terminal window. `workflow.status_and_next` may surface a
last-known sandbox summary but stays a high-level orientation endpoint.

The default backend is `lambda_labs`. Backend selection is controlled by
`RESEARCH_PLUGIN_EXECUTION_BACKEND`; supported values are `lambda_labs`, `modal`,
and `fake` (tests). Lambda Labs exposes the VM's normal SSH endpoint and needs
only `LAMBDA_LABS_API_KEY` (region/instance type are chosen per request, with
optional `RESEARCH_PLUGIN_LAMBDA_REGION` / `RESEARCH_PLUGIN_LAMBDA_INSTANCE_TYPE`
fallbacks). Modal exposes SSH over an unencrypted Modal tunnel
(`unencrypted_ports=[22]`). The registry generates a per-experiment SSH keypair
and authorizes its public key in the sandbox/VM. File sync is provider-neutral
SSH rsync owned by `SandboxService`. The execution contract (`SandboxBackend`)
stays narrow so additional providers can live inside `execution/backends/`; a
backend advertises whether it `requires_hardware_selection` (bundled SKUs) and
may expose an optional `hardware_catalog()` that powers `sandbox.options` and the
`needs_selection` menu.

`time_limit` is enforced. Modal sandboxes self-terminate at their server-side
timeout; for backends without server-side lifetime (Lambda Labs VMs, which
otherwise bill until manually killed), the daemon runs a background **reaper**
that terminates any running sandbox past its `expires_at` (a best-effort final
rsync runs first so results survive). The reaper polls every
`RESEARCH_PLUGIN_SANDBOX_REAPER_INTERVAL` seconds (default 30) and can be
disabled with `RESEARCH_PLUGIN_SANDBOX_REAPER=0`.

Core HTTP/service calls still require an explicit `project_id`. In project-local
MCP sessions, the proxy supplies that scope from hidden repo context and removes
`project_id` from agent-facing schemas. Agents should call `project.current`
first; if it returns `exists: false`, ask the user what project name and summary
to use before creating the folder's project.

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
- sandboxes

No resource version table is needed for v0.1. Store the last observed file token
directly on the resource row and append a lightweight event when it changes.
