# MCP Server Contract

This document describes the current agent-facing MCP architecture. The live
schemas and descriptions generated from `src/merv/brain/surface/tools/contracts.py` are the
authoritative per-field contract; `tools/list` is the authoritative catalog for
the active deployment.

## Authority and topology

The brain is the authority for durable research state and workflow policy. Every
agent client — local Claude Code, cloud Codex, Replit, browser-driven — connects
the same way: directly to the brain's stateless `POST /mcp` HTTP endpoint,
authenticated by a project-scoped key sent as `Authorization: Bearer <key>`. The
committed config files (`.mcp.json`, `.mcp.codex.json`, `mcp.json`) use
`type:"http"`, `url:"https://experiments.rapidreview.io/mcp"`, and
`headers.Authorization:"Bearer ${MERV_MCP_KEY}"`; the key is read from the
`MERV_MCP_KEY` env var and is never inlined into a committed file.

A key binds one immutable project. The gateway injects that project's id into
project-scoped calls and hides `project_id` from agent schemas. Agents never send
`repo_root`; the brain never receives a checkout root.

The normal session bootstrap is:

```text
project(action="current")
project(action="connect", project_id=... | name=..., summary=...)?
workflow.status_and_next(experiment_id?)
```

`project(action="current")` returns the bound project or `exists: false`.
`action="connect"` is the only operation where a caller-selected project id is
authoritative: the brain validates the existing project, or creates one from
`name` and `summary`. `action="overview"` reads every claim and experiment for
the bound project. `action="create"` creates a project without selecting it.

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
litreview.view               litreview.edit
litreview.cite
artifact.submit              artifact.find
storage.find                 storage.object
review.request               review.start               review.submit
sandbox.options              sandbox.get
sandbox.release              sandbox.extend
sandbox.runs                 sandbox.terminal
feed.register                feed.list
```

Every tool is a control tool served by the brain; the data-plane tool set is
empty. `storage.submit`, `storage.fetch`, and `feed.post` are control tools that
return a one-line command the agent runs to move bytes over a presigned URL;
`sandbox.request`, `sandbox.attach`, and `sandbox.pull_outputs` are served by the
brain.

Storage is optional. When no object store is configured, every `storage.*` tool
is omitted instead of advertising an unavailable feature.

These tools remain dispatchable for HTTP views but are hidden from agent
`tools/list`:

```text
project.get                  project.update              project.list
claim.list                   experiment.list             reflection.list
storage.put_object           storage.complete_upload
review.status
sandbox.list                 sandbox.health
```

The single `_tool_manifest.json` is generated from these same contracts and is the
sole checked-in catalog. Because every tool is brain-served, `tools/list` is
unavailable until the brain responds.

## Project scope

The project is fixed by the bearer key, so a project-scoped call can never target
another project. Supplying an arbitrary `project_id` to a project-scoped tool does
not switch projects: the gateway removes it and injects the key-bound id.

Core services never infer an active project. Scope injection exists only at the
gateway.

## Artifact submissions

`artifact.submit {target_type, target_id, role, path, lens_id?, title?}` is a
control tool: the brain validates legality and workflow-state guards, mints a
pending artifact with a one-time upload token, and returns
`{artifact_id, run}` where `run` is a ready-to-run
`curl -sf -T <path> '<base>/api/artifacts/u/<token>'` line the agent executes
verbatim. The token-bearer PUT enforces the role byte cap, pins the bytes, and
(for gated markdown) returns one follow-up `run` line per relative image link.
Bytes travel over the agent's own shell, never through the brain or MCP.

Workflow lints and reviews read the submitted bytes, never a later live edit.
There is no background checkout scan. Resubmit a changed file to replace the
slot (a new artifact id is minted, invalidating review snapshots).

`artifact.find(artifact_id=...)` resolves one artifact; without `artifact_id`,
it lists the project's complete artifacts filtered by target and role.

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

The declarative table in `src/merv/brain/research_core/domain/workflow_gates.py` drives enforcement,
`allowed_transitions`, gate checklists, and `workflow.status_and_next`.

- `submit_design` requires a pinned `plan` artifact with the required section
  spine.
- `mark_ready_to_run` requires a passing design review for the current snapshot.
- `submit_results` requires current-attempt `result`, `report`, and `graph`
  artifacts. It generates the attempt's system metrics exhibit and pins it when
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

External tools and target types use **reflection**. Persisted ids keep the
`syn_` prefix. The statuses are:

```text
reflecting -> synthesizing -> reflection_review -> published
```

`abandoned` is terminal. One wave may be open per project.

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
compact outputs into the local experiment folder before artifact submission,
and use durable object storage for heavy files.

Provider behavior is capability-shaped:

- Lambda Labs (default) and Thunder Compute expose fixed instance types and may
  return `needs_selection` with a live hardware menu.
- Modal composes GPU/CPU/memory directly.
- `fake` is used by tests.

Provisioning is best-effort synchronous. `sandbox.request` may return
`provisioning`; poll with `sandbox.get`, never repeated request calls. Long work
uses `merv_run`; `sandbox.runs` reports durable run receipts. Transcript and run
lookups are sandbox-scoped even when addressed through an experiment.

`sandbox.release` is a two-step destructive operation: the first call returns a
retention checklist, and `confirm_retained=true` terminates the machine. Release
or expiry destroys anything not explicitly retained.

## MLflow, storage, and feed

- `mlflow.context(experiment_id?)` returns the centralized tracking endpoint,
  namespace, and environment for direct MLflow clients.
- `mlflow.finalize_run` closes or refreshes the plugin-associated run.
- `storage.submit` and `storage.fetch` return a one-line command the agent runs
  to transfer bytes over a presigned URL; `storage.find` and `storage.object`
  operate on the brain's ledger.
- `feed.post` returns a one-line command to upload any captured image or HTML
  embed; feed registration and reads are brain control operations.

## HTTP transport and errors

The brain exposes `/mcp/tools` and `/mcp/call`, plus the stateless `/mcp`
endpoint every agent client connects to. It rejects `repo_root` context. Byte transfers no
longer ride MCP: a tool returns a command that hits a one-time token endpoint
(`/api/artifacts/*`, `/api/storage/u/*`, `/api/feed/u/*`) directly.

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

The checkout never contains the brain database. There is no machine-local routing
state; project files remain ordinary checkout files until explicitly submitted.

See [ARCHITECTURE.md](ARCHITECTURE.md),
[WORKFLOW_AND_REVIEW.md](WORKFLOW_AND_REVIEW.md), and
[ARTIFACT_MODEL.md](ARTIFACT_MODEL.md) for the corresponding system contracts.
