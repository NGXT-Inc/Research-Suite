# MCP Server Contract

This document describes the current agent-facing MCP architecture. The live
schemas and descriptions generated from `backend/tools/contracts.py` are the
authoritative per-field contract; `tools/list` is the authoritative catalog for
the active deployment.

## Authority and topology

The brain is the authority for durable research state and workflow policy. The
agent client launches a local stdio MCP proxy that:

- resolves the research checkout;
- maps that checkout to a brain project id;
- merges the brain and local tool catalogs;
- injects project scope into project-scoped calls;
- forwards control tools to one brain URL;
- executes checkout-sensitive data tools locally.

The proxy never forwards `repo_root`. Brain services and HTTP routes receive an
explicit `project_id`; the proxy hides that field from agent schemas when the
machine-local checkout link supplies it.

The normal session bootstrap is:

```text
project(action="current")
project(action="connect", project_id=... | name=..., summary=...)?
workflow.status_and_next(experiment_id?)
```

`project(action="current")` returns the linked project or `exists: false`.
`action="connect"` is the only operation where a caller-selected project id is
authoritative: the proxy validates the existing project, or creates one from
`name` and `summary`, and then stores the local folder link. `action="overview"`
reads every claim and experiment for the linked project. `action="create"`
creates a project without linking the folder.

## Tool catalog

The agent-visible control tools are:

```text
workflow.status_and_next
project
claim.create                 claim.update
experiment.create            experiment.get_state
experiment.transition        experiment.exhibit
mlflow.context               mlflow.finalize_run
reflection.create            reflection.get
reflection.transition
resource.find
storage.find                 storage.object
review.request               review.start               review.submit
sandbox.options              sandbox.get
sandbox.release              sandbox.extend
sandbox.runs                 sandbox.terminal
feed.register                feed.list
```

The proxy-local data tools are:

```text
experiment.materialize_folders
resource.register
storage.upload_file          storage.download_file
sandbox.request              sandbox.attach              sandbox.pull_outputs
feed.post
```

Storage is optional. When no object store is configured, every `storage.*` tool
is omitted instead of advertising an unavailable feature.

These tools remain dispatchable for HTTP views or proxy composition but are
hidden from agent `tools/list`:

```text
project.get                  project.update              project.list
claim.list                   experiment.list             reflection.list
resource.delete
storage.put_object           storage.complete_upload
review.status
sandbox.list                 sandbox.health
```

The proxy's checked-in local catalog is regenerated from the same contracts and
tested byte-for-byte. If the brain is unreachable, `tools/list` can still expose
the local half; control tools are unavailable until the brain responds.

## Project scope

An unlinked checkout cannot call project-scoped tools. The proxy returns
`project_not_linked` and instructs the agent to use `project(action="connect")`.
Supplying an arbitrary `project_id` to another project-scoped tool does not
switch projects: the proxy removes it and injects the linked id.

Core services never infer an active project. Scope inference exists only in the
proxy adapter.

## Resource submissions

`resource.register` has three modes:

```text
resource.register(path=..., kind=..., title?=...)
resource.register(paths=[...], kind=..., title?=...)
resource.register(resource_id=..., target_type=..., target_id=..., role=...)
```

The first two modes may also include the complete association trio
`target_type`, `target_id`, and `role`. The trio is all-or-none.

The proxy resolves and bounds each path, observes mtime/ctime/size/content type,
computes SHA-256, and submits the facts. For gated roles it also submits
size-capped bytes and referenced figures. The brain stores append-only version
records and pins each association to an exact version and attempt.

Workflow lints and reviews read the submitted bytes, never a later live edit.
There is no background checkout scan. Re-register a changed file to submit a new
version.

`resource.find(resource_id=..., include_history=true)` resolves one resource and
its observed versions. Without `resource_id`, it lists with filters and
pagination.

## Experiment workflow

The agent-facing statuses are:

```text
planned -> design_review -> ready_to_run -> running -> experiment_review -> complete
```

`failed` and `abandoned` are terminal exits. The typed transitions are:

```text
submit_design
mark_ready_to_run
start_running
retry_running
submit_results
complete
mark_failed
abandon
```

The declarative table in `backend/domain/workflow_gates.py` drives enforcement,
`allowed_transitions`, gate checklists, and `workflow.status_and_next`.

- `submit_design` requires a pinned `plan` resource with the required section
  spine.
- `mark_ready_to_run` requires a passing design review for the current snapshot.
- `submit_results` requires current-attempt `result`, `report`, and `graph`
  resources. It generates the attempt's system metrics exhibit and pins it when
  runs are found, or when a plugin-created run proves the attempt was quantitative
  but MLflow is unavailable. When pinned, the report must reference and interpret
  it.
- `complete` requires a passing experiment review for the current snapshot.
- `retry_running` is a same-attempt infrastructure retry and remains `running`.

A result-review rejection must return to `running` when the approved plan still
stands, or to `planned` with a new attempt when the design is flawed.

`workflow.status_and_next` returns a deliberately slim orientation view:
project summary, current experiment, gate, allowed/blocked actions, missing
evidence, review substate, and next action. The HTTP UI uses richer service views.

## Reflection workflow

External tools and target types use **reflection**. Persisted ids still use the
`syn_` prefix, and some response keys/internal services retain synthesis naming.
The agent-facing statuses are:

```text
reflecting -> synthesizing -> reflection_review -> published
```

`abandoned` is terminal. The domain/store name for `reflection_review` is
`synthesis_review`; projection adapters expose the reflection vocabulary to MCP.
One wave may be open per project.

- `reflection.create` snapshots the corpus and requires exactly five lenses:
  `amplify`, `avoid`, `entropy`, and two project-specific lenses.
- `submit_reflections` requires a separately submitted, non-empty
  `reflection_lens_doc` for every roster lens.
- `submit_reflection_artifacts` requires a valid `project_graph`, concise
  `reflection_doc`, and materializable `change_spec`.
- `publish` requires a passing `reflection_reviewer` review, then applies claim
  changes and creates one to three planned experiments from the reviewed spec.

A rejection returns to `synthesizing` when the lens documents stand, or to
`reflecting` with a new attempt when the fan-out must be repeated.

## Review sessions

Supported reviewer roles are `design_reviewer`, `experiment_reviewer`,
`reflection_reviewer`, `human`, and `automated_check`. The three workflow gates
use their matching reviewer roles.

The current protocol is:

```text
review.request(target_type, target_id, role, reason?, producer_session_id?)
review.start(review_request_id, reviewer_capability, caller_session_id, declared_agent?)
review.submit(review_session_id, verdict, synopsis, return_to?, notes?, findings?, evidence?)
```

For the three workflow reviewer roles, `review.request` validates the active
gate. `human` and `automated_check` are gate-exempt and may be requested outside
a workflow review gate. Every request pins a target snapshot, stores a hash of
the capability, and returns the plaintext capability once with
`reviewer_handoff.spawn_prompt`. Requesting a fresh capability supersedes prior
open requests for the same target and role.

`caller_session_id` is required at `review.start` and must differ from the
producer session. Start returns the submitted gated artifacts for the pinned
snapshot. A capability remains startable while the request is `requested` or
`started` and the capability is unexpired; the first accepted submission closes
the request and prevents other sessions from submitting.

`review.submit` requires a plain-language `synopsis`. Rejected experiment-attempt
and reflection reviews require `return_to`; design-review rejections always
return to `planned`. Rejection immediately routes the target state. A passing
review satisfies a workflow gate only when its role matches that gate and its
snapshot is current; `human` and `automated_check` passes do not replace the
required workflow reviewer. A pass does not perform the target's next
transition.

Reviewer skills impose the read-only operating role. The dispatcher rejects
other mutations that explicitly carry a `review_session_id`, but the system does
not authenticate every read or unrelated call as that reviewer. Session
separation is therefore a workflow boundary, not cryptographic model identity.

## Sandboxes

Sandboxes are project-scoped machines. They may be standalone, attached to
multiple experiments, and addressed by `sandbox_uid`. An experiment may have
multiple active sandboxes.

`sandbox.request` requires a caller-owned OpenSSH public key. The brain records
and authorizes the public key; caller private-key material never enters brain
state. The response and `sandbox.get` expose SSH facts such as host, port, and
user. The agent client constructs and runs SSH commands. `sandbox.pull_outputs`
requires a caller-supplied `key_path` when pulling retained files.

The sandbox workdir is machine-owned, independent of experiment attachment, and
defaults under `/workspace`; provider-specific `MERV_*_WORKDIR`
settings can change the root. Files are not synchronized automatically. Pull
compact outputs into the local experiment folder before resource registration,
and use durable object storage for heavy files.

Provider behavior is capability-shaped:

- Lambda Labs (default) and Thunder Compute expose fixed instance types and may
  return `needs_selection` with a live hardware menu.
- Modal composes GPU/CPU/memory directly.
- `fake` is used by tests.

Provisioning is best-effort synchronous. `sandbox.request` may return
`provisioning`; poll with `sandbox.get`, never repeated request calls. Long work
uses `rp_run`; `sandbox.runs` reports durable run receipts. Transcript and run
lookups are sandbox-scoped even when addressed through an experiment.

`sandbox.release` is a two-step destructive operation: the first call returns a
retention checklist, and `confirm_retained=true` terminates the machine. Release
or expiry destroys anything not explicitly retained.

## MLflow, storage, and feed

- `mlflow.context(experiment_id?)` returns the centralized tracking endpoint,
  namespace, and environment for direct MLflow clients.
- `mlflow.finalize_run` closes or refreshes the plugin-associated run.
- `storage.upload_file` and `storage.download_file` transfer checkout files via
  the local proxy; `storage.find` and `storage.object` operate on the brain's
  ledger.
- `feed.post` runs locally because it may capture a checkout image or HTML embed;
  feed registration and reads are brain control operations.

## HTTP transport and errors

The brain exposes `/mcp/tools` and `/mcp/call`. It rejects `repo_root` context and
direct MCP calls to data-plane tools. The proxy uses private `/api/data-plane/*`
submission routes for validated observations and local-data results.

Tool responses are tool-specific dictionaries; there is no universal mutation
envelope. Domain validation and workflow failures remain MCP protocol errors.
Transient transport failures are returned as error tool results so clients do
not disable the entire server:

- `brain_not_running` for an unreachable loopback brain;
- `cloud_unreachable` for a remote brain;
- `daemon_bad_response` (a retained legacy error-code spelling) for an invalid
  brain payload.

## Persistence

The brain selects its record and blob adapters at composition time:

- local preset: SQLite and local-directory blobs under the brain state root;
- control preset: Postgres and an S3-compatible submitted-byte blob store;
- optional heavy-object storage: a separate S3-compatible bucket.

The checkout never contains the brain database. The proxy owns only
machine-local routing state in `project_links.sqlite`; project files remain
ordinary checkout files until explicitly registered.

See [ARCHITECTURE.md](ARCHITECTURE.md),
[WORKFLOW_AND_REVIEW.md](WORKFLOW_AND_REVIEW.md), and
[RESOURCE_MODEL.md](RESOURCE_MODEL.md) for the corresponding system contracts.
