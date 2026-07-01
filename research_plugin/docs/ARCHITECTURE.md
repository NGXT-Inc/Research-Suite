# Research Plugin Architecture

## Purpose

`research_plugin` is a Codex plug-in that replaces the heavy backend with a
small local research kernel exposed through MCP.

The product model remains:

- Claim: what we think
- Experiment: what we try
- Resource: a repo file we use or produce

Everything else exists to help Codex and humans mutate that model correctly.

## Design thesis

The old backend had too many first-class subsystems for the MVP: resource
versions, artifact refs, manifests, workflow persistence, agent telemetry,
operation review, audits, execution, and API read models.

The plug-in architecture keeps the hard boundary but shrinks the implementation:

- Codex performs reasoning, editing, local scripting, and lightweight checks.
- Codex skills define the operating procedure.
- The MCP server owns durable memory and validates mutations.
- The MCP server owns Modal/Lambda sandbox provisioning for ML execution.
- The MCP server tells Codex the current status and next allowed workflow action.
- Design review and full experiment review are separate read-only reviewer roles.
- Reviewers submit structured reviews to MCP; MCP decides whether the gate passes.

## Components

```mermaid
flowchart TD
  User["User"] --> Codex["Codex"]
  Codex --> Skills["Research skills"]
  Codex --> LocalRepo["Local repo files"]
  Codex --> MCPProxy["research-plugin MCP proxy (stdio, stateless)"]

  Browser["Browser UI"] --> Daemon["research-plugin HTTP daemon"]
  MCPProxy --> Daemon

  Daemon --> Memory["Research memory (SQLite)"]
  Daemon --> Policy["Permission and workflow policy"]
  Daemon --> Jobs["Sandbox registry (Modal/Lambda + SSH)"]
  Daemon --> ResourceIndex["Repo-file resource index"]
  Daemon --> ReviewGates["Review gates"]

  Skills --> DesignReviewer["Design reviewer agent"]
  Skills --> ExperimentReviewer["Experiment reviewer agent"]
  DesignReviewer --> MCPProxy
  ExperimentReviewer --> MCPProxy
  ResourceIndex --> LocalRepo
```

## Process topology

The plugin is split across two processes per research repo:

1. **HTTP daemon** — long-lived. Owns SQLite, the activity log, the sandbox
   execution backend, and the sandbox registry/reapers. Exposes the full tool
   surface at `/mcp/*` and a UI-flavored view at `/api/*`.
2. **MCP stdio proxy** — short-lived, spawned by Codex on demand. Stateless.
   Discovers the daemon URL via `$REPO/.research_plugin/daemon.json` or
   `RESEARCH_PLUGIN_DAEMON_URL` and forwards `tools/list` / `tools/call` over
   HTTP.

The daemon must be running before Codex makes any tool call. Multiple MCP
proxies (e.g. parallel Codex sessions or reviewer agents) talk to the same
daemon, which serializes mutations through its in-process locks.

### Cloud topology — control / data plane split (IMPLEMENTED)

The same code base runs in three process roles selected by
`RESEARCH_PLUGIN_MODE` (see `backend/config.py`):

- **`local` (default)** — today's single-process topology, both planes in one
  process. Byte-identical to before the migration; tier-1 supported forever.
- **`control`** — the cloud **control plane**: multi-tenant records, gates,
  lifecycle, blob store, quotas, auth, and daemon task endpoints. It never
  touches a user checkout.
- **`daemon`** — the slim user-machine **data plane**: key custody, file
  observation, SSH command material, and the machine-local repo-folder to
  hosted-project link
  registry. It dials the control plane over HTTP (the cloud never dials in).

The split is built end-to-end. The load-bearing rule — *the cloud cannot see a
user's local filesystem* — is what puts file IO and SSH key material on the daemon and
everything else (orchestration, records, credentials, authz, cost governance)
on the control plane. The module-by-module assignment is in
**`docs/CONTROL_DATA_PLANE_SPLIT.md`**; operating the control plane (modes, env,
cleanup jobs, version floor, deploy) is **`docs/CONTROL_PLANE_OPERATIONS.md`**.
For client VM setup, use **`docs/HOSTED_CLIENT_QUICKSTART.md`**.

## Ownership

Codex owns:

- understanding the user's research intent
- reading and editing repo files
- writing scripts and lightweight experiment code
- running local commands when cheap and safe
- asking MCP for project memory, experiment status, and next action
- launching separate reviewer agents when MCP requires design or experiment review
- registering and associating retained local files as resources
- reading MCP-returned resource paths directly from the local repo on later turns

MCP owns:

- project memory for claims, experiments, and resources
- permissioned mutation checks
- experiment state machine and next-action guidance
- Modal/Lambda sandbox provisioning for expensive or long-running ML work
- resource registration and file observation
- resource associations to claims, experiments, reviews, and attempts
- review records and required gates
- reviewer capability tokens and read-only review sessions
- final acceptance/rejection of proposed state changes

## Simplified data model

```text
Project
  Claim[]
  Experiment[]
  Resource[]
  Review[]
  Event[]
```

Claim:

- statement
- scope
- status: draft | active | supported | weakened | contradicted | abandoned
- confidence: low | medium | high
- grounds: links to experiments/resources/reviews

Experiment:

- question or intent
- tested_claim_ids
- status: idea | planned | design_review | ready_to_run | running | experiment_review | complete | failed | abandoned
- plan file resource
- result file resources
- review records
- conclusion proposal
- attempts with prior plans, runs, reviews, and revision context

Resource:

- repo-relative file path
- kind/role
- last observed version token: `path + mtime_ns + size_bytes`
- optional git commit pointer
- associations to experiments, claims, reviews, and attempts

Review:

- target: experiment plan | experiment attempt | claim update
- role: design_reviewer | experiment_reviewer | human | automated-check
- reviewer identity: server-issued review session and capability
- verdict: pass | fail | needs_changes
- notes and required follow-up

Event:

- append-only history of accepted mutations and workflow milestones

## Mutation model

All meaning-changing mutations go through MCP tools.

Codex may edit files locally, but the research state is not changed until MCP
accepts a mutation.

Examples:

- create claim
- create experiment
- link experiment to claim
- register resource file
- mark experiment running
- record sandbox result
- record design review
- record experiment review
- propose claim status change
- accept experiment conclusion

The MCP server should return structured responses:

```json
{
  "ok": true,
  "state_changed": true,
  "requires_review": false,
  "next_action": "launch_experiment_reviewer",
  "message": "Experiment result file registered. Launch experiment review next."
}
```

## Workflow model

The workflow should stay simple but server-directed:

```text
idea -> planned -> design_review -> ready_to_run -> running -> experiment_review -> complete
            ^             |                                  |
            |             v                                  v
            +------ needs_changes                    needs_changes
            |                                                |
            +---------------- planned with revision context --+

failed / abandoned are terminal exits.
```

The server decides which transitions are allowed. Codex first asks the large
orientation question:

```text
workflow.status_and_next(project_id, experiment_id?)
```

In project-local MCP sessions, the stdio proxy supplies `project_id` from the
current repo context, so the agent-facing schema can omit it:

```text
workflow.status_and_next(experiment_id?)
```

The server answers with a project/experiment summary, the current gate, allowed
actions, blocked actions, missing evidence, and the next required step.

Core services never guess the active project. Every project-scoped service call
is explicit; the project-local MCP proxy is the adapter that fills that explicit
scope from repo context.

This tool is deliberately high-level. It can include a known sandbox summary from
durable state, but it should not poll Modal or perform detailed inspection itself.
Codex should use narrower tools when it needs fresh execution details.

Detailed tools exist for deeper inspection:

```text
project.get(project_id)
experiment.get_state(project_id, experiment_id)
sandbox.get(project_id, experiment_id)
sandbox.terminal(project_id, experiment_id)
```

Possible next steps include:

- write experiment plan
- launch design reviewer
- revise plan from design review feedback
- request a sandbox and run the experiment over SSH
- retain outputs and register resources
- launch experiment reviewer
- revise plan from experiment review feedback
- propose claim update
- complete experiment

If experiment review fails, the experiment returns to `planned`, but MCP carries
forward prior run context: previous plan, result resources, failed review
findings, and guidance about what should stay the same versus change.

## Sandbox execution

There is no job abstraction. Codex requests a sandbox and runs commands on it
directly over SSH. A sandbox can be standalone or attached to an experiment; it
can later be attached to another experiment without recreating the VM.

```text
Codex
  -> sandbox.request (MCP)
      -> SandboxService registry  (project sandbox, optional experiment attachment)
          -> SandboxBackend (Modal/Lambda)  ->  sandbox/VM + SSH endpoint
  -> ssh <command>  (run by Codex itself, recorded to the experiment transcript)
  -> explicit copy/upload of retained outputs before release
```

MCP owns policy, state, and visibility:

- gate `sandbox.request` on experiment status (`ready_to_run` / `running`)
- own per-sandbox SSH facts and the durable `sandboxes` row
- procure / reuse / release sandboxes and reconcile liveness
- expose the terminal transcript for visibility
- tell Codex when output files should be retained and registered as resources

`execution` owns the `SandboxBackend` implementations. The default backend is
**Lambda Labs** (VM-backed GPU execution); Thunder Compute and Modal are also
supported; `fake` is used for tests. Backends only procure SSH-reachable
machines and expose lifecycle/observability hooks. Backends declare a
`requires_hardware_selection` capability and may expose an optional
`hardware_catalog()`: Thunder and Lambda bundle GPU+CPU+RAM into fixed instance
types, so `SandboxService` returns a live availability menu (`needs_selection`)
when `sandbox.request` omits the `instance_type`; Modal composes the machine from
`gpu`/`cpu`/`memory` and needs no selection step.

## Reviewer identity and independence

Local reviewer identity cannot rely on IP addresses or machine boundaries. The
MVP should model identity as server-issued workflow capability:

1. Main Codex asks MCP for a review request.
2. MCP creates `review_request_id` and a one-time `reviewer_capability`.
3. The capability is scoped to one target, one role, read-only inspection tools,
   and `review.submit` for that request.
4. Main Codex spawns a separate reviewer agent with the appropriate review skill
   and passes the capability plus target context.
5. The reviewer starts a review session with MCP and submits the review directly.
6. MCP rejects reviews from the same producer session, expired capabilities,
   wrong role, wrong target, or capabilities minted before the target snapshot.

This is not cryptographic proof that two independent local minds were involved.
It is the practical local boundary: separate review assignment, separate tool
scope, immutable target snapshot, session lineage, and audit trail. For stronger
assurance, MCP can mark a review as `unverified_agent_review` and require human
review for high-risk gates.

## Plugin skills

The primary skill should make Codex follow the research loop:

1. inspect memory through MCP
2. clarify claim or experiment intent
3. create or update experiment plan through MCP
4. edit local files as needed
5. run lightweight checks locally
6. request a sandbox from MCP and run long work on it over SSH
7. retain outputs and register/associate local files as resources
8. launch design or experiment reviewer agent when MCP requires it
9. ensure the reviewer submits review directly to MCP
10. propose claim/experiment updates through MCP
11. ask MCP for next action until terminal

The review skills should make reviewer agents adversarial but bounded:

- design review checks whether the planned experiment can test the claim
- experiment review checks implementation, outputs, metrics, and conclusion
- both inspect only via read-only context/tools
- return a structured verdict
- submit the review to MCP
- never mutate project state directly

## MVP exclusions

Do not include in v0.1:

- artifact object store
- content-addressed manifests
- generic backend REST API
- browser UI
- multi-project server
- OAuth
- complex RBAC
- Temporal-style workflow engine
- broad automatic claim rewriting
- directory resources
