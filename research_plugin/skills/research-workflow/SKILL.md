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

## The experiment folder

Every experiment owns exactly one folder: `experiments/<name>/`
(created for you by `experiment.create`). Everything the experiment is lives
there — plan.md, scripts, configs, results, report.md, graph.json. This
matters because the folder is also the **sandbox sync unit**: when a sandbox
is provisioned, the whole folder is pushed to the VM, and while the sandbox
lives the folder is mirrored back to the local repo. Work you keep in the
folder from planning onward (the plan, the code you wrote for the run, the
notes the run needs) is therefore already on the VM when it boots. Shared
repo material (papers/, notes/) is NOT pushed — copy the specific files a run
needs into the experiment folder.

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
     When that happens, **poll `sandbox.get` every 30-60 seconds**
     (`poll_after_seconds`) until `status` is `"running"` (then use
     `ssh.command`) or `"failed"` (read `error`, fix the cause, and call
     `sandbox.request` again). A fresh Lambda Labs VM commonly takes **5-15
     minutes** to boot and bootstrap. Do **not** spam `sandbox.request` to
     poll, and do not treat `provisioning` as an error - the sandbox is
     still coming up.
2. When `status` is `"running"` the response includes `ssh.command` - a short
   dispatcher invocation like
   `.research_plugin/sbx <experiment_id>` that wraps all the SSH boilerplate
   (key, port, host, options). Run your experiment by appending a command, e.g.
   `.research_plugin/sbx <experiment_id> 'cd <workdir> && python train.py'`.
   Prefer this over retyping a full `ssh` line every call - it is short and is
   regenerated each request, so it always points at the live sandbox. Run it
   from the repo root; if you are elsewhere, use `ssh.raw_command` (the fully
   qualified `ssh` line) instead. Output streams back to you and is recorded to
   the experiment's terminal transcript for the user. Commands run under a
   tmux supervisor on the sandbox, so they keep running if SSH drops or your
   command call times out - a timeout means you stopped watching, not that the
   command stopped. Long foreground runs are safe; check the transcript
   (`sandbox.terminal`) for the command's `(exit N)` marker before re-running
   anything long.
3. The sandbox starts with your local experiment folder
   (`experiments/<name>/`) pushed to `$RP_EXPERIMENT_DIR`
   (`/workspace/<name>`). The push obeys the sync limits: files over
   100 MB and caches/checkpoints/archives (.git, venvs, `*.pt`, `*.ckpt`,
   `*.safetensors`, tarballs, ...) are skipped, except under
   `artifacts_to_keep/` (5 GB per-file cap). The request/get response reports
   how many files were pushed — 0 means the local folder had nothing eligible,
   so the remote folder starts empty.
4. While the sandbox is live, **the sandbox owns the experiment folder**. It is
   mirrored back to the local repo every few seconds and on `sandbox.sync`, as
   an exact replica: deletions and renames on the VM propagate locally, and
   local edits are overwritten. So make ALL experiment-file edits on the VM
   over SSH — including report.md and graph.json, whose local copies (and the
   user's live UI) update through the mirror.
5. The experiment folder is the **only** synced location. Keep datasets,
   caches, temporary checkpoints, and anything else you do not want carried
   into the repo OUTSIDE the folder — `$RP_DATASET_DIR` (`/workspace/data`) is
   the conventional home. Nothing outside the folder is ever synced, and it
   dies with the VM; anything reusable (preprocessing/analysis scripts,
   configs, small manifests, compact outputs) belongs inside the folder. Put
   deliberate large final artifacts under `$RP_EXPERIMENT_DIR/artifacts_to_keep`.
   Prefer to write a Markdown data note in the experiment folder (for example
   `data.md`) describing datasets used, source identifiers, split/filter
   choices, important columns, row counts, caveats, and where large ephemeral
   files were placed outside the folder — future sandboxes get the folder
   pushed again, so the note carries the data context forward.
6. If the sandbox response includes `environment.available_tokens` with
   `HF_TOKEN`, Hugging Face credentials are already available inside SSH
   commands. Use them through Hugging Face tooling or environment variables; do
   not print the token, write it into files, or sync it as a resource.
   **Training observability.** Every sandbox runs an MLflow tracking server and
   a TensorBoard side-by-side (their state lives outside the experiment folder
   and is preserved by the daemon — you never manage it). Inside SSH commands,
   `MLFLOW_TRACKING_URI=http://localhost:5000` is already exported and
   `$RP_TB_LOGDIR` points at the TensorBoard logdir. Log run params, metrics,
   and artifacts to MLflow, and write TensorBoard events to `$RP_TB_LOGDIR`.
   Framework integrations such as Hugging Face `Trainer` and PyTorch Lightning
   `MLFlowLogger` can use these env vars directly; for plain PyTorch, add
   `mlflow.autolog()` when useful. The user sees the dashboards live in their
   UI; you do not need to fetch, share, or open the URLs yourself.
7. Before registering or associating result resources, call
   `sandbox.sync(experiment_id)`. This mirrors `$RP_EXPERIMENT_DIR` back to the
   local experiment folder with SSH rsync. Resource tools only see local files,
   so remote result paths are not valid resources until this sync completes.
   The backend also syncs automatically every few seconds, so the user is
   already seeing your latest files — the explicit call is the durable handoff
   before workflow mutations.
8. Use `sandbox.terminal(experiment_id)` to re-read the transcript,
   `sandbox.get` to check status, and `sandbox.release` to shut the sandbox down
   when finished (it also expires automatically at `time_limit`). Call
   `sandbox.sync` before `sandbox.release` whenever you need files from the
   sandbox.

Do not embed secrets in commands. Treat the sandbox as ephemeral: durable
outputs must be written inside `$RP_EXPERIMENT_DIR`, synced with
`sandbox.sync`, and then registered/associated as resources.

## Experiment creation

Prefer the minimal MCP shape:

```json
{
  "name": "lora-rank-sweep",
  "intent": "One concise statement of what the experiment will test.",
  "tested_claim_ids": ["claim_..."]
}
```

`name` is **required**: a short, folder-safe name (letters, digits, `.`, `_`,
`-`; max 48 characters) that becomes the experiment folder
`experiments/<name>/` — everything the experiment is (plan, code, results,
report, graph) lives there, and that folder is what syncs to sandboxes. Names
are unique within a project: if the name is already taken, creation is
rejected and you must pick a new one.

The create response confirms the folder: it includes `folder` (e.g.
`experiments/lora-rank-sweep/`), already created on disk. Work inside it from
that moment on — starting with `plan.md`.

Pick the name for **navigation**: the project supplies the shared context, so
the name should carry only the contrast — lead with what distinguishes this
experiment from its siblings, and do not repeat the project topic. In a LoRA
replication project, `released_adapters` / `scratch_training` /
`paper_only_rebuild` scan instantly; `lora_glue`, `lora_glue_scratch`, and
`lora_glue_paper_only` all read as the same experiment until the last word.

`intent` is the durable **one-line headline** — the experiment's title in the
UI. The full design (hypothesis, method, evaluation, risks) does **not** go in
`intent`; it lives in the `plan.md` resource (see Experiment plan below). The
MCP server still accepts the older aliases `claim_id`, `claim_ids`, `title`,
`hypothesis`, `design`, `success_criteria`, and `risks`, but they are deprecated
and no longer folded into `intent` — put that content in the plan instead.
Create always starts at `planned`. Use `experiment.transition` for workflow
state changes.

## Experiment plan

The plan is one repo file in the experiment folder
(`experiments/<name>/plan.md`) associated with role `plan`. It is the
**face of the experiment**: what the user reads in the UI
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

If `submit_design` is rejected for missing sections, fill them in and
**re-associate the plan** (`resource.associate` with role `plan`) before
retrying — the lint reads the bytes you SUBMITTED at associate time, never the
live file, so an edit counts only once it is re-associated.

## Results report

The report is one repo file in the experiment folder
(`experiments/<name>/report.md`) associated with role `report`. It is
the **face of the executed experiment**: what the
user reads in the UI once results exist and what the experiment reviewer
grades against the plan's Evaluation section. Write it from
`skills/research-workflow/report-template.md`, in the same pass as your result
files — save the figures (matplotlib PNGs) while the run's metrics are at hand.

`experiment.transition(submit_results)` is blocked until the current attempt
has BOTH a `result` resource and a `report` resource whose SUBMITTED content
(the bytes captured when you associate it) passes the report lint:

- **Summary**, **Results**, **Deviations from plan**, **Conclusion** headings
  with real content.
- **Results must contain a markdown table** of metrics: target/paper value vs
  achieved, per task/seed where relevant — the exact metrics the plan's
  Evaluation section named.
- **Under 10 KB.** The report is the executive layer: link raw metrics files
  (`results.json`, logs) as separate result resources instead of inlining.
- **Every relative image link has submitted figure content.** Save figures
  next to the report (`figures/*.png`), `sandbox.sync` so they exist locally,
  and THEN associate the report — associating it submits the figures it links
  alongside it. Added a figure later? Re-associate the report.

The Conclusion must apply the plan's pre-registered decision rule explicitly —
the experiment reviewer compares the two documents side by side.

## Logic graph

The logic graph is one JSON repo file in the experiment folder
(`experiments/<name>/graph.json`) associated with role `graph`. It is a
**qualitative story you write about the logical path of the experiment** —
the critical questions that needed answers, the hard decisions and the
reasoning behind them, the pivots (including those forced by reviews), and
what was learned — a small DAG the user explores in the UI during and after
the run. Write it from `skills/research-workflow/graph-template.md`.

This is not an event-driven graph. Events may be mentioned as anchors for
reasoning, but the structure is logic: question → decision → consequence →
lesson. It is NOT a pipeline or provenance diagram — if your nodes are
components and your edges read `produces`/`contains`/`records`, you have
drawn dataflow, not the story. And it is not a generated artifact: do not
build it with a script over your result files; choosing what mattered is the
authorship, so write the JSON yourself.

You design the graph. Node `kind` names, edge labels, and structure are yours;
the template's vocabulary is illustrative, not required. What deserves a node
is an editorial call — record what shaped the experiment, not every step. If a
development adds no valuable information to the story, you may leave it out.

Keep nodes brief and use `refs` for depth: a node's `refs` array takes
repo-relative paths of synced files or record ids (`res_…`, `rev_…`,
`claim_…`, `exp_…`), and the UI resolves them into links the user and
reviewer can follow. Point a problem node at the log that shows it, a pivot
node at the review that forced it, an outcome node at the results file —
instead of restating their contents in `detail`.

`experiment.transition(submit_results)` is blocked until the current attempt
has a role-`graph` resource whose SUBMITTED content passes the envelope lint: valid
JSON (`version: 1`), every node with a unique `id` and non-empty `label`,
**at most 16 nodes**, edges referencing existing nodes and forming a DAG, file
under 16 KB. The lint checks shape only; the experiment reviewer judges
whether the story is honest and consistent with the report and transcript.

Start the graph early and sync it as the story develops — the user watches it
live, and a hard decision is best recorded in the moment you make it, while
the reasoning is fresh; a graph reconstructed at the end keeps the events but
loses the *why*. After a review rejection, consider whether the rejection and
the rework it forces belong in the story. If the graph is at the 16-node
budget and something important must be added, reduce the graph first; how to
retell the story within the budget is your call.

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
  `reports`, or `output` (the singular roles are `result`, `report`, and
  `graph`)
- gates and lints judge the SUBMITTED bytes (pinned at `resource.associate`),
  never the live working tree: after fixing a gated artifact (plan, report,
  graph, proposals, reflection), re-associate it to submit the fix — editing
  the file alone changes nothing the workflow can see
- do not create artifact manifests or content-addressed resource objects
- do not restore old versions through MCP; edit the live file normally and
  re-associate it to submit a new version

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
`workflow.review_gate.skill`. For design reviews, this is `experiment-design-review`. For
full experiment reviews, this is `experiment-attempt-review`. For project synthesis
reviews, this is `project-reflection-review`. Use your client's
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
The reviewer's `return_to` decides where the experiment lands, with revision
context attached either way:

- Design-review rejections always return to `planned` — revise the plan and
  resubmit for design review (the attempt counter advances).
- Experiment-review rejections return to `planned` when the plan itself was
  flawed, or to `running` when the plan stands but execution or the conclusion
  was flawed. Back in `running`, the approved plan and current attempt remain
  valid: address the revision context, re-run what is needed, sync results,
  and `submit_results` again — do not redo the plan or design review.

## Project reflection

The project also has a level above experiments: a **project logic graph** —
one living JSON file, the current logic state of the whole project under the
same 16-node budget — maintained through gated **reflection waves**
(`synthesis.create / get / list / transition`). A wave fans out five
differentiated reflection subagents, reconciles their reflections into the
project graph plus a what's-next proposals file, and passes a synthesis
review before publishing. When `workflow.status_and_next` carries a
`project_reflection` block — either an open wave's guidance or a soft
"Consider running a project reflection" staleness hint — see the
`project-reflection` skill. When the project is idle (no active
experiments) and at least one experiment has finished since the last
published synthesis, the project-level call goes further and suggests the
reflection as the next action (`current_gate: reflection_suggested`) — the
natural moment to distill what was learned before starting the next
experiment. The nudge is advisory either way: creating the next
claim/experiment stays allowed, and whether new developments change the
project's logic state is your call.

## Completion

Before marking an experiment complete:

- resources are synced
- design and experiment reviews are recorded and accepted by MCP
- conclusion is grounded in files or sandbox outputs
- MCP accepts the transition

If MCP rejects a mutation, follow its `next_action` rather than working around it.
