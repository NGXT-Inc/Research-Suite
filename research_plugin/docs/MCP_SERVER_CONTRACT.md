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
projects from other folders and does not create a project as a side effect. When
`exists` is true, it also returns a compact `at_a_glance`: a one-line summary
of how old the latest reflection is, recent experiments and claims capped at 5
each, the latest reflection/project-graph resource ids, ids for finished
experiments or claim changes since that reflection, active experiment ids, and
any open reflection id. If `exists` is false, the agent should
ask the user what project name and summary to use before calling
`project.create`, unless the user already supplied that information.

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
experiment.create(project_id, name, intent, tested_claim_ids?)
experiment.get(project_id, experiment_id)
experiment.get_state(project_id, experiment_id)   # see "get_state shape" below
resource.list(project_id, kind?, experiment_id?, missing?, compact?, limit?, offset?)
review.status(project_id, target_type, target_id)
event.list(project_id, limit?)
```

`experiment.create` is intentionally simple in durable storage: it creates a
planned experiment with a short unique `name`, one `intent` string, and
optional linked claims. `name` is required and folder-safe (letters, digits,
`.`, `_`, `-`; max 48 chars): it becomes the experiment folder
`experiments/<name>/`, which data-plane actions can materialize locally and
which is also the folder synced to sandboxes. Names are unique per project
(case-insensitive) — proposing a name that already exists is rejected with "an
experiment named '<name>' already exists in this project — choose a new name".
The create response confirms the folder in-band: alongside the experiment state
it carries `folder`
(`experiments/<name>/`) and a one-line `folder_guidance` telling the agent to
work inside it from the start. The MCP schema advertises `name`,
`intent`, and `tested_claim_ids` as the preferred shape, but the server
accepts common Codex/user aliases:

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

`experiment.transition(submit_results)` is gated on three current-attempt
resources: a `result` file, a `report` file passing the report lint, and a
logic graph (role `graph`, see `skills/research-workflow/graph-template.md`) —
the agent-authored story of the experiment's decisions, problems, and pivots
as a DAG. The graph lint checks only the envelope: valid JSON (`version: 1`),
unique node ids with non-empty labels, **at most 16 nodes**, edges that
reference existing nodes and form a DAG, file under 16 KB. Vocabulary,
structure, and what deserves a node are the agent's editorial calls; the
experiment reviewer judges the story's substance.

### Reflection tools (project reflection waves)

```text
reflection.create(project_id, title?, lenses)   # lenses: exactly 5 (3 core + 2 authored)
reflection.get(project_id, reflection_id)
reflection.list(project_id)
reflection.transition(project_id, reflection_id, transition)
```

A reflection wave (`syn_…`) is the project-level reflection record:
`reflecting → synthesizing → reflection_review → published` (plus `abandoned`).
One wave may be open per project. `reflection.create` validates the roster
envelope (the core lens ids `amplify`/`avoid`/`entropy` plus two
authored lenses, each with `charter` + `why_distinct`) and snapshots the
corpus. `submit_reflections` requires a current-attempt role-`reflection_lens_doc`
resource named `<lens_id>.md` for every roster lens — each submitted by its
own subagent. `submit_reflection_artifacts` requires the project logic graph (role
`project_graph`, the same `graph_lint` envelope as experiment graphs — ≤16 nodes,
DAG), a concise reflection document (role `reflection_doc`), and a
materializable change spec (role `change_spec`). The change spec is the
reviewed belief-state update: `claim_changes` plus a decision of either
`hard_stop` or `create_experiments` with 2-3 planned experiments. The
reflection document is a 16 KB markdown artifact and may include relative image
links; linked image bytes are captured when the document is associated.
`publish`
requires a passing `reflection_reviewer` review at the current snapshot; only
then does it apply claim changes and either mark the project stopped or create
the approved planned experiments, while also pinning the published graph
version. When publish creates planned experiments, the response includes
`post_publish_guidance` with the new experiment folders and recommended next
calls: `experiment.materialize_folders(status="planned")`, then
`workflow.status_and_next(experiment_id=...)` for the first new experiment.
Resource associations on reflection waves are attempt-scoped, exactly like
experiments, so a `return_to: "reflecting"` rejection (attempt bump)
invalidates the prior reflections.

Graph node `refs` resolve `syn_` ids too, so experiment graphs and the
project graph can cross-link to the reflection that motivated them.
`reflection.get` includes `gate_checklist`, a machine-readable dashboard of the
current forward gate. During `reflecting` it lists one
`reflection_lens:<lens_id>` item per roster lens, with `present`/`missing`
coverage and the submitted path/version when available. During `synthesizing`
it lists the required `project_graph`, `reflection_doc`, and `change_spec`
resources, running the same pinned-byte validators that the transition uses so
invalid artifacts surface as checklist `problems`. During `reflection_review`
it exposes the `review:reflection_reviewer` item with `pending`, `requested`,
`started`, or `passed` status at the current review snapshot.
`reflection.get` includes `project_graph_diff`: when the current wave has a
submitted project graph and a previous published graph exists, it compares the
two pinned graph versions and reports added, removed, changed, and unchanged
node/edge groups. When either graph cannot be read, the block is present with
`available: false` and an explanatory `reason`/`problems` list.
`workflow.status_and_next` carries a `project_reflection` block while a wave
is open (slim wave state + gate guidance) or when the project has drifted
from the last published reflection (computed on read, never stored). Drift has
three levels: a soft "Consider running a project reflection…" hint once the
advisory threshold is crossed; an idle-project recommendation where the
workflow block becomes `current_gate: reflection_suggested`; and a hard create
block once five newly-terminal experiments have accumulated since the last
published reflection. At the hard threshold, `experiment.create` is removed
from `allowed_actions`, listed in `blocked_actions`, and rejected by
`experiment.create` until a project reflection is published. `claim.create`
can remain allowed. Explicitly experiment-scoped calls are never taken over,
but the `project_reflection` side block still carries the signal.

### Resource tools

```text
resource.register_file(project_id, path?, paths?, kind, title?)  # single file or batch
resource.validate(project_id, path, role)                         # preflight local lint
resource.associate_batch(project_id, associations=[{resource_id, target_type, target_id, role}, ...])
resource.resolve(project_id, resource_id, include_history?)       # include_history adds versions
results.merge_tsv(project_id, source_path, target_path, key_columns?, dry_run?)
```

The server observes local repo files by path, stores latest metadata in
`resources`, and records append-only observations in `resource_versions`.
Each observed version captures size, mtime, content sha256, and mimetype;
file content itself is not stored — historical content lives in the user's
own repo / git history.

`resource.validate` reads the current local file without registering or
associating it. For gated roles such as `plan`, `report`, and `graph`, it
preflights the same byte caps, required sections, report figure availability,
and graph envelope checks that would otherwise fail at association or
transition time.

When a resource is associated with an experiment, MCP stores the experiment's
current `attempt_index` and current `version_id` on that association. Workflow
gates only count resources from the current attempt, so stale result files from
a failed attempt cannot satisfy a rerun.

`resource.associate_batch` is a data-plane convenience wrapper over
`resource.associate`: rows are applied in order through the same role validation,
gated-artifact byte capture, and attempt scoping as single associations.

Gates and lints judge the bytes SUBMITTED at `resource.associate` (pinned to
a version and stored in the blob store), never the live working tree. There is
no background reconciliation: editing or deleting a file after association
changes nothing the workflow can see — re-associate the resource to submit the
new content. MCP does not scan the repo or register new files.

`results.merge_tsv` is a local safe-import helper for sandbox-produced result
ledgers. It parses TSV rows by stable key columns, skips identical duplicates,
atomically appends new rows, and refuses conflicting rows without modifying
`target_path`; use it instead of copying a partial remote `results.tsv` over a
full local ledger.

### Workflow tools

```text
workflow.status_and_next(project_id, experiment_id?)
workflow.next_action(project_id, experiment_id)
workflow.transition(project_id, experiment_id, transition, evidence?)
experiment.materialize_folders(project_id, experiment_id?, status?)
workflow.record_blocker(project_id, experiment_id, reason)
workflow.request_human_review(project_id, experiment_id, reason)
```

`experiment.materialize_folders` creates canonical local folders under
`experiments/<name>/` without changing experiment state. With no `experiment_id`
it defaults to planned experiments, which is the common case after reflection
publish materializes a new wave; pass `status: null` to create folders for every
experiment in the project.

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
    "revision_context": "",
    "warnings": [
      {
        "kind": "sandbox_expiry",
        "severity": "warning",
        "sandbox_uid": "sbx_...",
        "sandbox_id": "provider-id",
        "expires_at": "2026-06-03T05:11:37Z",
        "seconds_remaining": 1800,
        "message": "Active sandbox expires in about 30 minutes...",
        "recommended_actions": ["sandbox.get", "sandbox.terminal", "retain_outputs_before_release"]
      }
    ]
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
"sandbox_data_dir", "expires_at" }`.
For a `running` experiment with a live sandbox at or under one hour from
`expires_at`, the workflow block includes a `sandbox_expiry` warning so the
agent retains outputs before release or provider expiry destroys the VM.
If the sandbox is lost or execution is interrupted for infrastructure reasons
while the approved plan still stands, `allowed_transitions` includes
`retry_running`. Calling `experiment.transition` with that transition keeps the
experiment in `running`, preserves `attempt_index`, and stores the evidence in
`revision_context` so the next run is explicitly a same-attempt infrastructure
retry rather than a plan revision.
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
error. It also carries `gate_checklist`, a machine-readable view of the current
forward gate derived from the same workflow table, for example:

```json
{
  "status": "running",
  "transition": "submit_results",
  "leads_to": "experiment_review",
  "ready": false,
  "items": [
    { "id": "resource:result", "kind": "resource", "role": "result",
      "status": "present", "satisfied": true },
    { "id": "resource:report", "kind": "resource", "role": "report",
      "status": "valid", "validator": "report", "satisfied": true },
    { "id": "resource:graph", "kind": "resource", "role": "graph",
      "status": "invalid", "validator": "graph", "satisfied": false,
      "problems": ["..."] }
  ]
}
```

Review-gated statuses expose a `review:<role>` item with `status` of
`pending`, `requested`, `started`, or `passed`. Storage uploads that declare
`producing_experiment_id` appear as compact `storage_objects` references
alongside resources, with `{id, name, version, kind, content_sha256, size_bytes,
content_type, status, expires_at, producing_run, source_uri, notes}` for every
non-deleted storage object produced by the experiment. Once status is `running`
or later, `experiment.get_state` also includes the experiment-scoped `mlflow`
block and `mlflow_guidance`, matching the central tracking context returned by
`experiment.transition(start_running)` and `mlflow.context`. When the backend
MLflow write URI is configured, `start_running` creates the initial MLflow run;
state includes `mlflow_run`, and the `mlflow` block nests the same object as
`mlflow.run` while adding `MLFLOW_RUN_ID` / `RP_MLFLOW_RUN_ID` to `mlflow.env`.
Agents should resume that plugin-created run instead of creating a sibling run.
After the quantitative command exits, agents should call `mlflow.finalize_run`
before `submit_results`; the helper defaults to the persisted run id, sets a
terminal MLflow status when the backend write URI is available, performs a short
REST readback loop, and refreshes `mlflow_run.status` in experiment state.
Completed
experiments with tested claims and a conclusion include
`claim_update_suggestions`: conservative `claim.update` call skeletons scoped
by `project_id` and `claim_id`, with an inferred `suggested_status` only when
the conclusion text is clear enough. What
`experiment.get_state` drops is pure waste:

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
sandbox.request(experiment_id?, instance_type?, region?, gpu?, cpu?, memory?, time_limit?)
sandbox.pull_outputs(experiment_id? | sandbox_uid, paths?, destination_path?, overwrite?)
sandbox.get(experiment_id? | sandbox_uid)
sandbox.list()
sandbox.release(experiment_id? | sandbox_uid)
sandbox.terminal(experiment_id? | sandbox_uid, tail?, since?)   # cursor + running; poll with since=cursor for new output. Also last_command, last_exit_code / last_command_finished_at / command_running, and command_status_stale.
sandbox.health()
```

There is no job abstraction. Codex requests a sandbox, gets back SSH connection
details (including a short, ready-to-run `ssh.command`), and
runs shell commands on the sandbox itself. Lightweight work still runs locally.

`sandbox.request` is the procurement call. Sandboxes are project-scoped machines:
experiment attachment is optional, one sandbox can be attached to multiple
active experiments, and one experiment can have multiple live sandboxes. The
response carries `ssh` (host, port, user, key_path, command,
raw_command), `workdir`, `experiment_dir`, `local_experiment_dir`, `data_dir`,
`files_pushed` (how many files the initial folder push delivered; null while
unknown), `status`, `lifecycle_reason`, `lifecycle_detail`, `expires_at`,
`reused`, and — when set — the reserved hardware (`gpu`, `cpu`, `memory`,
`instance_type`, `region`).
By default, a request for a new experiment reuses and attaches the newest
confirmed-live sandbox in the same project before provisioning another VM; pass
`additional: true` to force a parallel sandbox for that experiment. Project
reuse responses include `reuse_source: "project_active_sandbox"`.

`ssh.command` is the short dispatcher form
`.research_plugin/sbx <sandbox_uid>` (run from the repo root); `ssh.raw_command`
is the full `ssh -i … user@host` line for use from any directory.

#### Hardware selection (provider-shaped)

Procurement differs by backend, and the **default backend is Lambda Labs**:

- **Lambda Labs (default)** sells fixed machine SKUs that bundle GPU + vCPU + RAM
  together, so the agent picks an `instance_type` rather than independent
  cpu/memory. When `sandbox.request` arrives with **no `instance_type`** and the
  project has **no live sandbox to reuse**, the server does **not** provision.
  It returns `status: "needs_selection"` with a live, cheapest-first `options`
  menu (each entry: `instance_type`, `gpu`, `gpu_count`, `vcpus`, `memory_gib`,
  `storage_gib`, `price_usd_per_hour`, `regions`). The agent re-calls
  `sandbox.request(experiment_id, instance_type=<choice>, region?=<choice>)`.
  Omit `region` to auto-pick a region that currently has capacity. On Lambda,
  `gpu` is a free-form *filter* over the menu and `cpu`/`memory` are ignored (the
  SKU fixes them).
- **Thunder Compute** exposes fixed GPU specs that bundle GPU + vCPU +
  RAM. When `sandbox.request` arrives with **no `instance_type`** and the
  project has **no live sandbox to reuse**, the server returns
  `status: "needs_selection"` with a live, cheapest-first `options` menu. The
  agent re-calls `sandbox.request(experiment_id, instance_type=<choice>)`.
  Thunder does not expose region selection through the current API.
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

There is no synced VM location. The sandbox's work folder is `experiment_dir`
(`/workspace/<name>`, exported inside SSH commands as `$RP_EXPERIMENT_DIR`;
`workdir` is the same path, and SSH commands start there). Files written there
stay on the VM until the agent explicitly copies selected outputs back to the
local checkout over SSH. Everything left on the VM dies with release or expiry.
`data_dir` (`/workspace/data`, exported as `$RP_DATASET_DIR` /
`$RP_SANDBOX_DATA_DIR`) is the conventional home for large datasets, caches,
checkpoints, parquet files, and heavy intermediates. Heavy artifacts that need
to survive should be uploaded through durable storage tools instead of copied
into the repo. Agents should also prefer to save a Markdown data note in the
local experiment folder (for example `experiments/<name>/data.md`) describing
datasets used, source
identifiers, split/filter choices, important columns, row counts, caveats, and
where large ephemeral files were placed outside the folder.

`sandbox.pull_outputs` is the explicit retained-output helper for light files
created under the remote `experiment_dir`. It copies selected repo-relative
files or directories into the local experiment folder over SSH/rsync; with no
`paths`, it first checks for common outputs (`results/`, `figures/`,
`report.md`, `graph.json`, `metrics.json`, `results.json`, `results.tsv`) and
pulls the ones that exist. Existing local files are preserved/refused unless
`overwrite: true` is supplied. After pulling files locally, use
`resource.register_file` / `resource.associate` for retained artifacts and
`results.merge_tsv` for remote `results.tsv` rows that need to enter a
canonical local ledger.

When the backend has `HF_TOKEN` in its env file or process environment,
`sandbox.request` / `sandbox.get` include an `environment.available_tokens`
entry naming `HF_TOKEN`. The token value is not returned. Inside SSH commands,
`HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN` are available for Hugging Face tooling.
The backend passes the token through Modal's sandbox `secrets` API, not as a
plain sandbox `env` value and not as a synced repo `.env` file.
Agents must not print the token, write it into synced files, or register it as a
resource.

When a fresh sandbox/VM is created, setup returns SSH details and a remote work
folder (`$RP_EXPERIMENT_DIR`). No files are copied automatically. Agents fetch
code/data on the box, keep disposable bulk data under `$RP_DATASET_DIR`, and
explicitly retain outputs before release: pull light files with
`sandbox.pull_outputs` or upload heavy artifacts with storage tools. Resource
tools only operate on local repo files, so a file produced remotely cannot be
associated until it has been copied back locally. Release and expiry destroy
the VM and any files the agent did not retain.

Provisioning is **best-effort-synchronous**. Creating a sandbox can outlast the
MCP call timeout (cold GPU, image/bootstrap work), so `sandbox.request` provisions on
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
Terminal rows include `lifecycle_reason` so `terminated` is explainable:
`user_release`, `expired`, `idle_timeout`, `provider_unreachable`, or
`terminated`; failed rows report `provisioning_failed` or
`provisioning_interrupted` with the error text in `lifecycle_detail`.

Visibility: every SSH command and its output are recorded to a per-experiment
transcript inside the sandbox. `sandbox.terminal` reads it live from the sandbox.
The response also persists and returns `last_command`, a compact snapshot of the
latest parsed command marker: command id, command text, started/finished times,
status (`running`, `succeeded`, `failed`, or `interrupted`), exit code, and a
capped output tail. If a later transcript read fails, `sandbox.terminal` still
returns that last-known command snapshot with `command_status_stale: true`, so
agents can recover status even when SSH is temporarily unavailable. The UI
renders the transcript as a terminal window. `workflow.status_and_next` may
surface a last-known sandbox summary but stays a high-level orientation
endpoint.

The default backend is `lambda_labs`. Backend selection is controlled by
`RESEARCH_PLUGIN_EXECUTION_BACKEND`; supported values are `thunder_compute`,
`lambda_labs`, `modal`, and `fake` (tests). Lambda Labs exposes the VM's
normal SSH endpoint and needs `LAMBDA_LABS_API_KEY` (or
`RESEARCH_PLUGIN_LAMBDA_API_KEY`; region/instance type are chosen per request, with optional
`RESEARCH_PLUGIN_LAMBDA_REGION` / `RESEARCH_PLUGIN_LAMBDA_INSTANCE_TYPE`
fallbacks). Thunder Compute remains available with `RESEARCH_PLUGIN_THUNDER_API_KEY`
(or `THUNDER_COMPUTE_API_KEY`). Modal exposes SSH over an unencrypted Modal tunnel
(`unencrypted_ports=[22]`). The registry generates a per-sandbox SSH keypair
and authorizes its public key in the sandbox/VM. Output retention is explicit:
the agent pulls selected light files back over SSH with `sandbox.pull_outputs`,
while heavy files should go through durable storage tools. The execution
contract (`SandboxBackend`) stays narrow so additional providers can live inside
`execution/backends/`; a
backend advertises whether it `requires_hardware_selection` (bundled SKUs) and
may expose an optional `hardware_catalog()` that powers `sandbox.options` and the
`needs_selection` menu.

`time_limit` is enforced. Modal sandboxes self-terminate at their server-side
timeout; for backends without server-side lifetime (Lambda Labs VMs, which
otherwise bill until manually killed), the daemon runs a background **reaper**
that terminates any running sandbox past its `expires_at`. The reaper polls every
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
review.request_and_start(project_id, target_type, target_id, role, reason?, declared_agent?, caller_session_id?)
review.start(review_request_id, reviewer_capability, declared_agent?)
review.submit(review_session_id, verdict, notes, findings, evidence?)
review.status(project_id, target_type, target_id)
```

Reviewer roles:

- `design_reviewer`: reviews experiment plan before execution.
- `experiment_reviewer`: reviews executed attempt, result resources, metrics,
  and conclusion before completion or claim update.
- `reflection_reviewer`: reviews a project reflection wave — the reflected
  project logic graph, concise reflection document, and change spec against the
  corpus and the five lens reflections — before publish. Rejections route via
  `return_to`:
  `synthesizing` (reflections stand) or `reflecting` (re-launch the fan-out).
- `human`: records a human decision with the same mechanism.
- `automated_check`: records deterministic checks or audit scripts.

Review targets are polymorphic: `target_type` is `experiment` or `synthesis`.
The same capability machinery applies to both — snapshot pinning covers the
target's status, attempt, and current-attempt resource versions.

Reviewers are read-only. A reviewer capability may only call read tools for the
target context plus `review.submit` for its own review request. Recording a
review does not automatically accept the underlying mutation unless policy says
the review satisfies the gate.

### Reviewer identity tools

```text
review.request(project_id, target_type, target_id, role, reason)
review.request_and_start(project_id, target_type, target_id, role, reason?, declared_agent?, caller_session_id?)
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

`review.request_and_start` is a convenience wrapper for the common agent
handoff path. It creates a request, immediately starts a read-only reviewer
session, and returns `review_request`, `review_session`, `review_request_id`,
and `review_session_id`. It intentionally does **not** return
`reviewer_capability`; use the separate `review.request` / `review.start` calls
when the plaintext capability must be handed to another process before session
creation.

`review.status` and the HTTP review queue expose the same `target_snapshot`
shape on review request and submitted review records. Frontends should use
`target_snapshot.resources[].version_id` for exact reviewed resource versions and
treat `target_snapshot_id` as an opaque backend fingerprint. Review request
records also include `recovery`: a non-secret hint for lost one-time
capabilities, for example `{capability_returned_once: true,
capability_available: false, can_request_fresh_capability: true, tool:
"review.request", arguments: {target_type, target_id, role}}`. The plaintext
reviewer capability is still returned only by `review.request` at creation time.

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
