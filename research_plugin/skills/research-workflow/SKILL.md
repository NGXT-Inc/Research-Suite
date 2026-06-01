---
name: research-workflow
description: >-
  Use when Codex should operate the Research Plugin workflow: ask MCP for status
  and next action, inspect claims, create or run experiments, sync repo-file
  resources, use MCP-controlled mutations, and launch read-only design or
  experiment reviewers when required.
---

# Research Workflow

Use the Research Plugin MCP server as the authority for research state and
workflow state.

## Core model

- Claim: what we think.
- Experiment: what we try.
- Resource: one regular file in the local repo.
- Review: read-only design, experiment, human, or automated judgment submitted
  to MCP.

Codex may freely work on local repo files. Do not treat those edits as
research-state mutations. A file becomes a research resource only after MCP
accepts a resource registration or sync and associates it with a claim,
experiment, review, or attempt.

## Workflow

1. Select or create the project first. Keep its `project_id` explicit.
2. Ask MCP for `workflow.status_and_next(project_id, experiment_id?)` before acting.
3. Identify the claim or experiment being worked on.
4. Follow MCP's `next_action`, allowed actions, blocked actions, and gate state.
5. Use MCP for all claim, experiment, resource, review, and workflow mutations.
6. Pass `project_id` on every project-scoped MCP call. Do not rely on defaults.
7. Edit local files only for implementation, notes, plans, configs, and results.
8. Run lightweight commands locally when safe.
9. For expensive or GPU work, request a sandbox with `sandbox.request` and run
   commands on it yourself over SSH (see Execution environment).
10. After execution, sync changed result files through MCP.
11. Launch a separate read-only reviewer agent when MCP requires design review or
   experiment review.
12. Make sure the reviewer submits directly to MCP using its review capability.
13. Propose conclusions or claim updates only after required resources and reviews exist.

If conversation memory is unclear, select the project id from `project.list`,
then ask MCP for `workflow.status_and_next(project_id, experiment_id?)` again.
Do not reconstruct workflow state from memory.

## Execution environment

Expensive or GPU work runs in a **Modal sandbox** that you drive directly over
SSH. There is no job abstraction — you run ordinary shell commands.

1. Once the experiment is `ready_to_run` (or `running`), call
   `sandbox.request(project_id, experiment_id, gpu?, cpu?, memory?, time_limit?)`.
   - `gpu` is an optional type such as `"A100"` or `"H100"`; omit it for a
     CPU-only sandbox. `time_limit` is the sandbox's max lifetime in seconds.
   - The registry keeps **one sandbox per experiment** and reuses the live one,
     so it is safe to call `sandbox.request` again to get the current details.
   - **Provisioning is best-effort-synchronous.** `request` returns SSH inline
     when the sandbox comes up quickly. If it can't finish in time (a large
     first sync or a cold GPU), it returns `status: "provisioning"` instead.
     When that happens, **poll `sandbox.get` every ~10s** (`poll_after_seconds`)
     until `status` is `"running"` (then use `ssh.command`) or `"failed"` (read
     `error`, fix the cause, and call `sandbox.request` again). Do **not** spam
     `sandbox.request` to poll, and do not treat `provisioning` as an error —
     the sandbox is still coming up.
2. When `status` is `"running"` the response includes `ssh.command` — a short
   dispatcher invocation like
   `.research_plugin/sbx <experiment_id>` that wraps all the SSH boilerplate
   (key, port, host, options). Run your experiment by appending a command, e.g.
   `.research_plugin/sbx <experiment_id> 'cd <workdir> && python train.py'`.
   Prefer this over retyping a full `ssh` line every call — it is short and is
   regenerated each request, so it always points at the live sandbox. Run it
   from the repo root; if you are elsewhere, use `ssh.raw_command` (the fully
   qualified `ssh` line) instead. Output streams back to you and is recorded to
   the experiment's terminal transcript for the user.
3. The repo is mounted at `workdir` on a shared Modal Volume; the daemon keeps it
   synced with the local repo. Write results under the repo so they sync back.
4. Use `sandbox.terminal(project_id, experiment_id)` to re-read the transcript,
   `sandbox.get` to check status, and `sandbox.release` to shut the sandbox down
   when finished (it also expires automatically at `time_limit`).

Do not embed secrets in commands. Treat the sandbox as ephemeral: durable
outputs must be written into the mounted repo and synced as resources.

## Experiment creation

Prefer the minimal MCP shape:

```json
{
  "project_id": "proj_...",
  "intent": "One concise statement of what the experiment will test.",
  "tested_claim_ids": ["claim_..."]
}
```

The MCP server also accepts common aliases such as `claim_id`, `claim_ids`,
`title`, `hypothesis`, `design`, `success_criteria`, and `risks`; those rich
fields are folded into the experiment's durable `intent`. Create always starts
at `planned`. Use `experiment.transition` for workflow state changes.

## Resource discipline

Resources are repo files. Prefer one file per resource.

When Codex creates or changes files during an experiment:

- identify the relevant repo-relative paths
- call the MCP resource sync/register tool
- associate synced resources with the current experiment, claim, or review
- when `workflow.status_and_next` includes `resource_guidance`, follow its
  `association_role`; do not guess plural role names such as `results`,
  `report`, or `output`
- expect the server to store path, mtime, size, current `version_id`, and
  backend-owned shadow Git metadata for small text files
- expect future status calls to return those paths so Codex can read the files
  directly from the repo
- expect `workflow.status_and_next` to refresh already-associated current-attempt
  resources if their live files changed; do not do separate sync checks before
  every workflow status call
- use `resource.history` to list immutable observed versions of a resource
  (historical file content is not retrievable through MCP — open the live file
  or use git history in the user's repo)
- do not create artifact manifests or content-addressed resource objects
- do not restore old versions through MCP; edit the live file normally and sync
  it as a new version

## Review discipline

When MCP says the next action is `launch_design_reviewer` or
`launch_experiment_reviewer`, inspect `workflow.review_gate`:

- `status: none`: call `review.request`, then launch a new reviewer agent using
  the returned `reviewer_capability` and `reviewer_handoff`.
- `status: requested`: a review request exists but no reviewer has started. Use
  the `reviewer_capability` from the last `review.request` response to launch
  the reviewer. If that capability is not available in context, call
  `review.request` again and use the fresh capability.
- `status: started`: the reviewer is active. Do not launch another reviewer;
  wait and check `review.status`.

Use the skill named by `reviewer_handoff.skill` or `workflow.review_gate.skill`.
For design reviews, this is `design-review`. For full experiment reviews, this
is `experiment-review`.

Give the reviewer the target claim, experiment id, relevant resource paths, the
review request id, and the reviewer capability. Reviewer agents must be separate
and read-only.

Reviewer agents are read-only. They may inspect context and submit their own
review to MCP. They must not mutate claims, experiments, resources, sandboxes,
or workflow state.

If a review fails or needs changes, ask MCP for `workflow.status_and_next`.
Expect the experiment to return to `planned` with revision context from prior
attempts and review findings.

## Completion

Before marking an experiment complete:

- resources are synced
- design and experiment reviews are recorded and accepted by MCP
- conclusion is grounded in files or sandbox outputs
- MCP accepts the transition

If MCP rejects a mutation, follow its `next_action` rather than working around it.
