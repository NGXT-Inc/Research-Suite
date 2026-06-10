---
name: research-workflow
description: >-
  Use when the agent should operate the Research Plugin workflow: ask MCP for
  status and next action, inspect claims, create or run experiments, sync
  repo-file resources, use MCP-controlled mutations, and launch read-only design
  or experiment reviewers when required.
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

You may freely work on local repo files. Do not treat those edits as
research-state mutations. A file becomes a research resource only after MCP
accepts a resource registration or sync and associates it with a claim,
experiment, review, or attempt.

## Workflow

1. Call `project.current` first. In project-local MCP this returns the project
   for the current folder, or `exists: false` if the folder does not have a
   project yet. If `exists` is false, do not invent a placeholder project. Ask
   the user what project name and short summary to use, unless the current user
   request already provided that information, then call `project.create`.
2. Ask MCP for `workflow.status_and_next(experiment_id?)` before acting.
3. Identify the claim or experiment being worked on.
4. Follow MCP's `next_action`, allowed actions, blocked actions, and gate state.
5. Use MCP for all claim, experiment, resource, review, and workflow mutations.
6. Do not invent project scope. Use the project-local MCP defaults, or pass the
   exact `project_id` returned by MCP when a schema requires it.
7. Edit local files only for implementation, notes, plans, configs, and results.
8. Run lightweight commands locally when safe.
9. For expensive local work, data inspection, data engineering, or GPU work,
   request a sandbox with `sandbox.request` and run commands on it yourself over
   SSH (see Execution environment). Prefer CPU-only sandboxes for data profiling
   and preprocessing unless the specific command needs GPU acceleration.
10. After execution in a sandbox, call `sandbox.sync(experiment_id)`
    before registering or associating result resources.
11. Launch a separate read-only reviewer agent when MCP requires design review or
   experiment review.
12. Make sure the reviewer submits directly to MCP using its review capability.
13. Propose conclusions or claim updates only after required resources and reviews exist.

If conversation memory is unclear, call `project.current` again. If `exists` is
true, ask MCP for `workflow.status_and_next(experiment_id?)`; if `exists` is
false, ask the user what project to create before calling `project.create`
unless they already supplied the project name and purpose.
Do not reconstruct workflow state from memory.

## Execution environment

Expensive or GPU work runs in a **cloud sandbox** that you drive directly over
SSH. You run ordinary shell commands. The default provider is **Lambda Labs**
(GPU VMs); Modal is also supported.

1. Once the experiment is `ready_to_run` (or `running`), call
   `sandbox.request(experiment_id, instance_type?, region?, gpu?, cpu?, memory?, time_limit?)`.
   - **Pick the hardware first (Lambda Labs).** Lambda sells fixed machine types
     that bundle GPU + CPU + RAM, so you choose an `instance_type`. If you call
     `sandbox.request` with no `instance_type` and there is no live sandbox yet,
     the response is `status: "needs_selection"` with an `options` menu of the
     machines available *right now* (cheapest first), each showing `instance_type`,
     `gpu`, `gpu_count`, `vcpus`, `memory_gib`, `price_usd_per_hour`, and
     `regions`. Re-call `sandbox.request(experiment_id, instance_type="<choice>")`
     to provision it; omit `region` to auto-pick one with capacity. You can also
     call `sandbox.options` anytime to browse availability without provisioning.
     Prefer the smallest/cheapest viable machine for data engineering and CPU
     work; pick a GPU SKU only when a command needs acceleration.
   - **Modal** instead composes the machine from the request: pass `gpu`
     (e.g. `"A100"`/`"H100"`, or omit for CPU-only), `cpu` (Modal CPU cores, 1
     core = 2 vCPUs), and `memory` (MiB). On Modal, `instance_type`/`region` are
     ignored.
   - `time_limit` is the sandbox's max lifetime in seconds (both providers).
   - The registry keeps **one sandbox per experiment** and reuses the live one,
     so it is safe to call `sandbox.request` again (even without `instance_type`)
     to get the current details — reuse skips the selection menu.
   - **Provisioning is best-effort-synchronous.** `request` returns SSH inline
     when the sandbox comes up quickly. If it can't finish in time (a large
     first sync or a cold GPU), it returns `status: "provisioning"` instead.
     When that happens, **poll `sandbox.get` every ~10s** (`poll_after_seconds`)
     until `status` is `"running"` (then use `ssh.command`) or `"failed"` (read
     `error`, fix the cause, and call `sandbox.request` again). Do **not** spam
     `sandbox.request` to poll, and do not treat `provisioning` as an error -
     the sandbox is still coming up.
2. When `status` is `"running"` the response includes `ssh.command` - a short
   dispatcher invocation like
   `.research_plugin/sbx <experiment_id>` that wraps all the SSH boilerplate
   (key, port, host, options). Run your experiment by appending a command, e.g.
   `.research_plugin/sbx <experiment_id> 'cd <workdir> && python train.py'`.
   Prefer this over retyping a full `ssh` line every call - it is short and is
   regenerated each request, so it always points at the live sandbox. Run it
   from the repo root; if you are elsewhere, use `ssh.raw_command` (the fully
   qualified `ssh` line) instead. Output streams back to you and is recorded to
   the experiment's terminal transcript for the user.
3. The sandbox starts with the experiment's local synced folder pushed to
   `$RP_SYNC_DIR` (`/workspace/synced`). Once it is running, treat the sandbox as
   the source of truth for experiment file changes: edit/run/write result files
   inside `$RP_SYNC_DIR` through SSH, not in the local repo.
4. Write compact scripts, configs, metrics, and result artifacts under
   `$RP_SYNC_DIR`. Download large datasets and caches to `$RP_UNSYNCED_DIR` /
   `$RP_SANDBOX_DATA_DIR` / `$RP_DATASET_DIR`, which is sandbox-local ephemeral
   storage that is not pulled back locally. Data transformations written only
   under `$RP_UNSYNCED_DIR` are ephemeral and will not be available to future
   sandboxes; keep the reusable preprocessing/analysis scripts, configs, small
   manifests, and final compact outputs under `$RP_SYNC_DIR` and call
   `sandbox.sync` so they persist locally. Put deliberate large final artifacts
   under `$RP_SYNC_DIR/artifacts_to_keep`. Prefer to write a Markdown data note
   in the experiment folder under `$RP_SYNC_DIR`
   (for example `experiments/<name>/data.md`) describing datasets used, source
   identifiers, split/filter choices, important columns, row counts, caveats,
   and where large ephemeral files were placed in `$RP_UNSYNCED_DIR`. This note
   should be synced so future sandboxes carry the data context forward.
5. If the sandbox response includes `environment.available_tokens` with
   `HF_TOKEN`, Hugging Face credentials are already available inside SSH
   commands. Use them through Hugging Face tooling or environment variables; do
   not print the token, write it into files, or sync it as a resource.
   **Training observability.** Every sandbox runs an MLflow tracking server and
   a TensorBoard side-by-side under `$RP_SYNC_DIR/.research_plugin_sessions`.
   Inside SSH commands,
   `MLFLOW_TRACKING_URI=http://localhost:5000` is already exported and
   `$RP_TB_LOGDIR` points at the TensorBoard logdir. Frameworks that
   auto-detect MLflow — Hugging Face `Trainer` with the default
   `report_to="all"`, and PyTorch Lightning with `MLFlowLogger` — pick it up
   with no setup. For plain PyTorch, add `mlflow.autolog()` once at the top of
   the training script. The user sees the dashboards live in their UI; you do
   not need to fetch, share, or open the URLs yourself.
6. Before registering or associating result resources, call
   `sandbox.sync(experiment_id)`. This pulls `$RP_SYNC_DIR` back to the local
   experiment folder with SSH rsync. Resource tools only see local files, so
   remote result paths are not valid resources until this sync completes. Also
   call `sandbox.sync` after major file changes so the user can inspect the
   latest files locally while the sandbox is still running.
7. Use `sandbox.terminal(experiment_id)` to re-read the transcript,
   `sandbox.get` to check status, and `sandbox.release` to shut the sandbox down
   when finished (it also expires automatically at `time_limit`). Call
   `sandbox.sync` before `sandbox.release` whenever you need files from the
   sandbox.

Do not embed secrets in commands. Treat the sandbox as ephemeral: durable
outputs must be written into `$RP_SYNC_DIR`, synced with `sandbox.sync`, and
then registered/associated as resources.

## Experiment creation

Prefer the minimal MCP shape:

```json
{
  "intent": "One concise statement of what the experiment will test.",
  "tested_claim_ids": ["claim_..."]
}
```

`intent` is the durable **one-line headline** — the experiment's title in the
UI. The full design (hypothesis, method, evaluation, risks) does **not** go in
`intent`; it lives in the `plan.md` resource (see Experiment plan below). The
MCP server still accepts the older aliases `claim_id`, `claim_ids`, `title`,
`hypothesis`, `design`, `success_criteria`, and `risks`, but they are deprecated
and no longer folded into `intent` — put that content in the plan instead.
Create always starts at `planned`. Use `experiment.transition` for workflow
state changes.

## Experiment plan

The plan is one repo file (e.g. `experiments/<name>/plan.md`) associated with
role `plan`. It is the **face of the experiment**: what the user reads in the UI
and what the design reviewer evaluates. Write it from
`skills/research-workflow/plan-template.md` (a PRD-style template).

The plan has a small **required spine** — `experiment.transition(submit_design)`
is blocked until each of these headings has real content:

- **Summary** — 2–3 plain sentences: what and why (the readable face).
- **Objective & hypothesis** — which claim, expected direction, and why it matters.
- **Evaluation** — how you will judge success: metric(s), baseline, decision
  rule, success threshold, and what would invalidate the result. This is the
  contract the experiment reviewer later grades the conclusion against.

The recommended sections (**Method**, **Outputs**, **Risks & confounders**) are
not lint-enforced, but the design reviewer can return `needs_changes` if they
are missing or too thin for this experiment. Scale their depth to the work.

If `submit_design` is rejected for missing sections, fill them in and retry —
the lint reads the live file, so no re-registration is needed (though you still
sync the plan as a resource so it is associated).

## Resource discipline

Resources are repo files. Prefer one file per resource.

When the agent creates or changes files during an experiment:

- identify the relevant repo-relative paths
- if the experiment ran in a sandbox, call `sandbox.sync` first; resource tools
  operate on local files and cannot associate unsynced remote sandbox files
- call the MCP resource sync/register tool
- associate synced resources with the current experiment, claim, or review
- when `workflow.status_and_next` includes `resource_guidance`, follow its
  `association_role`; do not guess plural role names such as `results`,
  `report`, or `output`
- expect `workflow.status_and_next` to refresh already-associated current-attempt
  resources if their live files changed; do not do separate sync checks before
  every workflow status call
- do not create artifact manifests or content-addressed resource objects
- do not restore old versions through MCP; edit the live file normally and sync
  it as a new version

## Review discipline

When MCP says the next action is `launch_design_reviewer` or
`launch_experiment_reviewer`, inspect `workflow.review_gate`:

- `status: none`: call `review.request`, then launch a new, independent reviewer agent using
  the returned `reviewer_capability` and `reviewer_handoff`.
- `status: requested`: a review request exists but no reviewer has started. Use
  the `reviewer_capability` from the last `review.request` response to launch
  the reviewer. If that capability is not available in context, call
  `review.request` again and use the fresh capability.
- `status: started`: the reviewer is active. Do not launch another reviewer;
  wait and check `review.status`.

Spawn an independent reviewer named by `reviewer_handoff.skill` or
`workflow.review_gate.skill`. For design reviews, this is `design-review`. For
full experiment reviews, this is `experiment-review`. Use your client's
subagent or skill-spawn mechanism — the name is identical in both. In Claude
Code, call the Agent tool with `subagent_type` set to this name (or
`research-plugin:<name>` if your client namespaces plugin subagents). In Codex,
invoke the skill of the same name.

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
