---
name: research-workflow
description: >-
  Use when the agent should operate the Merv workflow: ask MCP for
  status and next action, inspect claims, create or run experiments, register
  repo-file resources, use MCP-controlled mutations, and launch read-only design
  or experiment reviewers when required.
---

# Research Workflow

Use the Merv MCP server as the authority for research state and
workflow state.

## Core model

- Claim: what we think.
- Experiment: what we try.
- Reflection: what the project has learned across experiments.
- Agent-authored resource: one regular file in the local repo. The brain-created
  metrics exhibit is the system-authored exception.
- Review: read-only design, experiment, reflection, human, or automated
  judgment submitted to MCP.

You may freely work on local repo files. Do not treat those edits as
research-state mutations. A file becomes a research resource only after MCP
accepts a resource registration and associates it with a claim,
experiment, review, or attempt.

## Research process

Experiment workflow:

Plan -> Design Review -> Run Experiment -> Submit Results -> Experiment Review
-> Complete / Update Knowledge

Review loops:

- Design review can send work back to Plan.
- Experiment review can send work back to Run Experiment.
- Experiment review can send work back to Plan if the design itself was flawed.

Project reflection workflow:

Finished Experiments -> Reflection Wave -> Multiple Lens Reflections ->
Project Reflection -> Reflection Review -> Publish Project Logic + Next
Experiment Wave

Review loops:

- Reflection review can send work back to Project Reflection.
- Reflection review can send work back to Reflection Wave if the lens
  reflections need to be redone.

## Project reflection

The project also has a level above experiments: a living project logic graph,
maintained through reflection waves. When `workflow.status_and_next` includes
`project_reflection`, treat it as project-level work and use the
`project-reflection` skill for the reflection workflow.

Reflection drift starts advisory, then becomes a gate. The project is nudged to
reflect after the advisory threshold, but once the hard threshold is reached
(`workflow.status_and_next` reports `experiment_create_blocked`), `experiment.create`
is blocked until a project reflection is published. The published reflection's
reviewed change spec may create the next experiment wave. Claim creation can
still be allowed.

## Literature review

The project keeps one living literature review: a General Summary, dynamic
theme sections (each with a required TLDR), and a derived References list.
Whenever a paper informs a plan, claim, or conclusion: `litreview.cite` it to
the experiment/claim it supports, then make a *targeted* `litreview.edit` —
add or amend the one relevant section, never rewrite the document. Read
`litreview.view` (the TLDR outline) before editing so you know the document's
shape. `workflow.status_and_next` surfaces a soft `litreview` hint when cited
papers have not been worked into the review; it never blocks anything.

## Feed

The feed is your main line to the researcher. It is how they follow the work as
it happens — asynchronously, at a glance, without living in the dashboards or
the experiment table. Treat it like a social feed you author: bring them along
with brief, vivid posts at the moments that matter. Post the thing you looked
at — a labeled chart, a before/after, a tight code excerpt, an arxiv page, or
an interactive embed when the result has an explorable dimension — prose alone
is the fallback for the rare insight with no visible form, not the default.
Posts are short by design (a hard length cap), so each is one sharp idea, not
a paragraph.

Post when the work gets interesting: a result that surprises you, a pivot or a
kill, a bottleneck that finally broke, a dead end worth flagging, a hunch
you'd bet on. During a long-running experiment, bounded `kind="status"`
checkpoints threaded onto the experiment's arc keep spectators following
without breaking the finished-takeaway rule; tool responses may also carry a
one-line `feed_note` pointer worth a glance, never a command to clear. Use the
`feed-posting` skill for handle setup and the full craft — register once with
`feed.register`, then write one-idea posts with `feed.post`. Also check
`feed.list` for researcher reactions and replies on your recent posts, and
reply in a follow-up (`in_reply_to`) when a researcher's question deserves one.
It is never gated and never required — but a quiet feed leaves the researcher
in the dark, so keep it alive. The only things that don't belong are the boring
(a bare "exp done, acc 0.81" the table already shows) and the inflated (hype
you can't back with a number).

## The experiment folder

Every experiment owns exactly one folder: `experiments/<name>/`
(announced by `experiment.create`; call `experiment.materialize_folders` if the
local directory is missing). Everything the experiment is lives there —
plan.md, scripts, configs, results, report.md, graph.json. Resource tools only
see local repo files. A sandbox is just an ephemeral machine you SSH into: fetch
code and data on the box, write compact outputs under `$MERV_EXPERIMENT_DIR`, then
pull retained files back with `sandbox.pull_outputs` before registering them.
Heavy artifacts should go to durable object storage instead of into the repo.

## Workflow

1. Call `project` with `action: "current"` first. In project-local MCP this
   returns the project for the current folder, or `exists: false` if the folder
   does not have a project yet. If `exists` is false, do not invent a
   placeholder project. Ask the user whether to link an existing project (they
   give its project id) or start a new one (they give a name and short summary),
   unless the current user request already provided that information, then call
   `project` with `action: "connect"` — it validates or creates the project and
   links this folder to it. If
   `exists` is true, it returns the linked project identity. When you need the
   full project picture — every claim including settled or abandoned ones,
   every experiment including terminal ones — call `project` with
   `action: "overview"` rather than expecting an `at_a_glance` block from
   `current` or inferring state from `workflow.status_and_next`'s active-only
   view.
2. Ask MCP for `workflow.status_and_next(experiment_id?)` before acting.
3. Identify the claim or experiment being worked on. Before creating a new
   claim, check `project` `action: "overview"` so you do not recreate a
   settled or abandoned one; before creating an experiment, use overview to see
   the siblings (name the contrast with them, and do not recreate a dead one).
4. Follow MCP's `next_action`, allowed actions, blocked actions, and gate state.
5. Use MCP for all claim, experiment, resource, review, and workflow mutations.
6. Do not invent or pass project scope to normal agent-facing tools. Their
   schemas hide `project_id`; the local proxy discards any supplied value and
   injects the project linked to this checkout. The merged `project` tool is the
   exception: `action: "connect"` accepts the user-selected existing
   `project_id`, while `current` and `overview` take no agent-supplied scope.
7. Edit local files only for implementation, notes, plans, configs, and results.
8. Run lightweight commands locally when safe.
9. For quantitative ML work, follow Quantitative observability whether running
   locally or in a sandbox.
10. For expensive local work, data inspection, data engineering, or GPU work,
   request a sandbox with `sandbox.request` and run commands on it yourself over
   SSH (see Execution environment). Prefer CPU-only sandboxes for data profiling
   and preprocessing unless the specific command needs GPU acceleration.
11. After execution in a sandbox, explicitly pull retained files off the box
    before registering or associating result resources. Use `sandbox.pull_outputs`
    for light files, and storage tools for heavy files.
12. Launch a separate read-only reviewer agent when MCP requires design review or
   experiment review.
13. Make sure the reviewer submits directly to MCP using its review capability.
14. Propose conclusions or claim updates only after required resources and reviews exist.

If conversation memory is unclear, call `project` with `action: "current"`
again. If `exists` is true, ask MCP for `workflow.status_and_next(experiment_id?)`;
if `exists` is false, ask the user what project to link or create before calling
`project` with `action: "connect"` unless they already supplied that information.
Do not reconstruct workflow state from memory.

## Quantitative observability

For quantitative ML work — training, evaluation, sweeps, ablations, or any run
where metrics drive the conclusion — use MLflow for params, metrics, and
artifacts, and save compact plot/table evidence under the experiment folder.
Do not require MLflow for qualitative experiments, literature work, code-only
probes, or planning tasks.
Before a sandbox or local run, call `mlflow.context` with `experiment_id` or use
the `mlflow` block returned by `experiment.transition(start_running)`:

```sh
export MLFLOW_TRACKING_URI="<from mlflow.context.env>"
export MLFLOW_EXPERIMENT_NAME="<from mlflow.context.env>"
export RP_PROJECT_ID="<from mlflow.context.env>"
export RP_EXPERIMENT_ID="<from mlflow.context.env>"
# Optional: present when the plugin created the initial run at start_running.
export MLFLOW_RUN_ID="<from mlflow.context.env, if present>"
mkdir -p "$MERV_EXPERIMENT_DIR"/results "$MERV_EXPERIMENT_DIR"/figures
```

For local non-sandbox runs, call `mlflow.context` to get the central tracking
URI. Omit `experiment_id` when you need project-level navigation context and the
plugin experiment-to-MLflow-name map; include `experiment_id` when you need the
exact `merv/<project>/<experiment>` name and env vars for a run. A missing
`MLFLOW_TRACKING_URI` in your current shell is not evidence that the backend
lacks MLflow; fetch it with `mlflow.context`. Do not create a file-backed local
MLflow store just because the shell env is empty. `experiment.transition` to
`start_running` also returns that experiment-scoped `mlflow` block. To review or
compare runs, use MLflow's own programmatic APIs directly from the returned URI
and experiment names, e.g. `mlflow.set_tracking_uri(...)`,
`MlflowClient.search_runs(...)`, `MlflowClient.get_metric_history(...)`,
`MlflowClient.list_artifacts(...)`, and `MlflowClient.download_artifacts(...)`.
Plot comparisons yourself from those queries — a labeled figure you can analyze
and post to the feed. Do not create a file-backed local MLflow store as the
default tracking path for Merv experiments. If MLflow is unavailable,
say so in the report and still save compact result files.

For quantitative runs, resume the plugin-created run when `MLFLOW_RUN_ID` is
present; otherwise create one with MLflow's native API. Keep the MLflow run
identity lightweight. Log `project_id`, `experiment_id`, and a short
`run_purpose` / run group. If there is a clear primary metric, also log
`primary_metric` and `primary_metric_direction`.
Do not add git metadata or claim ids as a default MLflow requirement; claims are
traceable through the plugin experiment record, and git/data lineage can be added
later when the project explicitly needs it. Optional dataset or config notes are
fine when they are obvious and useful, but do not block the run on dataset
digests, dataset versioning, or config hashes.

Example MLflow identity pattern for a quantitative run:

```python
import os
import mlflow

run_purpose = "seed_0_baseline"

run_id = os.environ.get("MLFLOW_RUN_ID")
run = mlflow.start_run(run_id=run_id) if run_id else mlflow.start_run(run_name=run_purpose)
with run:
    mlflow.set_tag("project_id", os.environ["RP_PROJECT_ID"])
    mlflow.set_tag("experiment_id", os.environ["RP_EXPERIMENT_ID"])
    mlflow.set_tag("run_purpose", run_purpose)
    mlflow.set_tag("primary_metric", "validation_accuracy")
    mlflow.set_tag("primary_metric_direction", "max")
```

After the command exits, call `mlflow.finalize_run` before submitting results.
Omit `run_id` when `MLFLOW_RUN_ID` came from the plugin; pass
`status="FAILED"` or `"KILLED"` if execution did not finish successfully, or
`status=null` when the script already ended the MLflow run and you only need
canonical readback. This refreshes experiment state so stale immediate MLflow
`RUNNING` statuses do not confuse the report/review loop.

Do not make tracking stores the only submitted result. Save compact evidence
under the experiment folder, especially `results/*.json`, `results/*.csv`, and
`figures/*.png`, so `report.md` can cite files that can be registered and
reviewed.

### The metrics exhibit — the system writes the numbers, you write the meaning

At `submit_results` the system generates a **metrics exhibit** and pins it as a
system-authored resource (`experiments/<name>/metrics_exhibit.json`) when the
attempt has an observed MLflow run (or a plugin-created run cannot be read back
because MLflow is unavailable). Attempts with nothing quantitative to exhibit
get no exhibit and no exhibit-reference gate. A pinned exhibit is built from
observation, not from your account: up to the newest 50 MLflow runs in this
attempt's window under `merv/<project>/<experiment>` — the exhibit records when
that cap is reached — plus eligible pinned result JSON (`metrics.json`,
`results.json`, and `results/*.json` associated with role `result`), each entry
with provenance. Runs logged after `submit_results` remain in MLflow but are
outside the attempt's finalized exhibit.

Consequences:
- Whatever you log IS the record. Log every run to the MLflow env you were
  handed, tag `project_id`/`experiment_id`, and pull result files into the
  experiment folder before submitting — an empty or thin exhibit under a
  quantitative plan is a loud signal to the reviewer.
- Call `experiment.exhibit` BEFORE writing `report.md` and write the report
  around it. When it returns a pinned exhibit, reference
  `metrics_exhibit.json` and interpret it; the gate requires that reference
  whenever the exhibit exists. If an unconfigured/no-run fallback produces no
  exhibit, interpret the registered result evidence and explain the gap instead
  of inventing an exhibit link. Qualitative experiments with no runs get no
  exhibit and no extra machinery.

## Execution environment

Expensive or isolated work can run in a **cloud sandbox** that you drive directly over
SSH. Once the experiment is `ready_to_run` (or already `running`), generate or
select a caller-owned SSH keypair and call `sandbox.request(experiment_id?,
instance_type?, region?, gpu?, cpu?, memory?, time_limit?, public_key,
additional?)`, passing only the single-line OpenSSH public key. Keep the private
key local. Follow the returned `hint`; `sandbox.request`/`sandbox.get` are the
source of truth for provider selection, polling, expiry, SSH facts, and the
remote work folder. A sandbox can also be created unattached and addressed by
`sandbox_uid`; `additional: true` requests another machine instead of reusing
an experiment's attached live sandbox.

Use the smallest viable machine. On fixed-SKU providers such as Lambda Labs or
Thunder Compute, use `sandbox.options` or omit `instance_type` to get the live
machine menu; on Modal, request `gpu`/`cpu`/`memory` directly. If the response
is `needs_selection` or `provisioning`, follow it and poll `sandbox.get` after
`poll_after_seconds`; do not use repeated `sandbox.request` calls as a poll
loop.

When `status` is `running`, construct the SSH invocation from the returned
`ssh.host`, `ssh.port`, and `ssh.user` facts plus the caller-owned private key.
Some local enrichments may also return `ssh.command`, `ssh.raw_command`, or
`ssh.key_path` conveniences, but the split proxy does not guarantee them.

**Anything expected to run longer than ~5 minutes goes through `merv_run`** —
never babysit a long command over a foreground SSH channel or poll the
transcript for it. Launch it as
`ssh ... 'merv_run <label> -- <command>'` (e.g. `merv_run seed0 -- python train.py
--seed 0`): the run detaches, survives disconnects, logs to
`.runs/<label>/log.txt`, and writes an `exit_code` sentinel when it finishes.
Then either long-poll `sandbox.runs(wait_seconds=...)` within the session (one
slow call instead of N transcript polls; keep `wait_seconds<=45` unless your
client's tool timeout allows more), or simply end the turn and call
`sandbox.runs` when next attending the experiment. Every sandbox.* response
carries a compact `runs` line while runs exist. Labels are one-shot — pick a
new label per launch. Finished-run receipts survive box death, but logs and
outputs do not: pull what you need before release/expiry.

Use `sandbox.terminal(experiment_id)` to inspect transcript output
and the structured `last_command` status before re-running anything long. If
`command_status_stale` is true, the transcript read failed and `last_command` is
the last successful snapshot, which is still useful for recovery decisions.
If the sandbox died, expired, or the command was interrupted by infrastructure
while the approved plan still stands, call
`experiment.transition(transition="retry_running", evidence={...})` before
requesting or attaching the replacement sandbox. This keeps the same attempt and
records why execution is being rerun; use a planned retry only when the design
itself needs to change. The transition response carries the MLflow run to use:
a still-open run is resumed in place, while one you already finalized (for
example with `mlflow.finalize_run(status="FAILED")` after the crash) is
replaced by a fresh run — always take `MLFLOW_RUN_ID` from the retry response
rather than reusing an old value.

While the sandbox is live, make experiment-folder edits on the VM under
`$MERV_EXPERIMENT_DIR`. No files are copied automatically. Keep datasets, caches,
temporary checkpoints, and other disposable bulk files under `$RP_DATASET_DIR`.
Keep durable scripts, configs, notes, compact outputs, report figures/tables,
and deliberate final artifacts under `$MERV_EXPERIMENT_DIR` so you can pull them
off deliberately before release.

Use the centralized MLflow env from `mlflow.context` /
`experiment.transition(start_running)` inside the SSH command that performs the
run. Sandbox provisioning does not automatically export MLflow env vars, and
sandbox responses are not the source of truth for tracking configuration. Save
compact evidence under `$MERV_EXPERIMENT_DIR`.

Before registering or associating result resources, call `sandbox.pull_outputs`
for light retained files, passing the caller-owned private `key_path` when
`sandbox.get` did not return an `ssh.key_path` enrichment. Upload heavy artifacts
with `storage.upload_file` when durable storage is enabled. Resource tools only
see local repo files, so remote sandbox paths are not valid resources until you
have pulled the files back locally. Do this before `sandbox.release`; release
and expiry destroy the VM and anything you did not retain. Release is two-step:
the first call returns a retention checklist without deleting, and only a second
call with `confirm_retained: true` terminates the machine.

Do not embed secrets in commands or retained files. Treat the sandbox as
ephemeral: durable outputs must be explicitly copied or uploaded and then
registered/associated as resources.

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
report, graph) lives there, and it is the local destination for retained
sandbox outputs. Sandbox files are not synchronized automatically. Names are
unique within a project: if the name is already taken, creation is rejected and
you must pick a new one.

The create response announces the folder: it includes `folder` (e.g.
`experiments/lora-rank-sweep/`). Data-plane actions create the directory on
demand — call `experiment.materialize_folders` if you want it on disk before
the first file write. Work inside it from that moment on — starting with
`plan.md`.

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
`hypothesis`, `design`, `success_criteria`, and `risks`, but they are
deprecated: `title` and friends only backfill an empty `intent` (they are not
concatenated into it) — put that content in the plan instead.
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
- **Objective & hypothesis** — which claims, expected direction, and why it matters.
- **Evaluation** — how you will judge success: metric(s), baseline, decision
  rule, success threshold, and what would invalidate the result. This is the
  contract the experiment reviewer later grades the conclusion against.

The recommended sections (**Method**, **Outputs**, **Risks & confounders**) are
not lint-enforced, but the design reviewer can return `needs_changes` if they
are missing or too thin for this experiment. Scale their depth to the work.

Plans may include figures: every relative image link must resolve to a local
file under 5 MB when you register the plan — a dangling or oversized link is
rejected at `resource.register`, and `submit_design` re-checks that each
linked figure was submitted alongside the plan.

If `submit_design` is rejected for missing sections, fill them in and
**re-register the plan** (`resource.register` with role `plan`) before
retrying — the lint reads the bytes you SUBMITTED at register time, never the
live file, so an edit counts only once it is re-registered.

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
- **When a metrics exhibit is pinned, Results must reference it**
  (`metrics_exhibit.json`) and interpret it — every run it shows, using the
  exact metrics the plan's Evaluation section named. Do not hand-copy numbers
  into your own table as the record; the exhibit is the record (see The metrics
  exhibit above). Preview it with `experiment.exhibit` before writing. If no
  exhibit is produced, interpret the registered result evidence and explain
  the MLflow/no-run gap instead.
- **Under 16 KB.** The report is the executive layer: link raw metrics files
  (`results.json`, logs) as separate result resources instead of inlining.
- **Every relative image link has submitted figure content.** Save figures
  next to the report (`figures/*.png`), copy them off the sandbox so they exist
  locally, and THEN associate the report — associating it submits the figures it
  links alongside it. Added a figure later? Re-associate the report.

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
repo-relative paths of registered files or record ids (`res_…`, `rev_…`,
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

Start the graph early and keep a local copy current as the story develops —
the user watches it live, and a hard decision is best recorded in the moment
you make it, while the reasoning is fresh; a graph reconstructed at the end
keeps the events but loses the *why*. After a review rejection, consider
whether the rejection and the rework it forces belong in the story. If the
graph is at the 16-node budget and something important must be added, reduce
the graph first; how to retell the story within the budget is your call.

## Resource discipline

Agent-authored resources are repo files. Prefer one file per resource; the
brain-created metrics exhibit is not something the agent registers.

When the agent creates or changes files during an experiment:

- identify the relevant repo-relative paths
- if the experiment ran in a sandbox, pull retained files off the box with
  `sandbox.pull_outputs` first; resource tools operate on local files and
  cannot associate remote sandbox files
- call `resource.register` with the local `path` (or `paths` for a batch) and
  the association trio (`target_type`, `target_id`, `role`) to register the
  file(s) and associate them to the current experiment, claim, or review in one
  call; to associate an already-registered resource, pass its `resource_id`
  with the trio instead of a path
- when `workflow.status_and_next` includes `resource_guidance`, follow its
  `association_role`; do not guess plural role names such as `results`,
  `reports`, or `output` (the singular roles are `result`, `report`, and
  `graph`)
- gates and lints judge the SUBMITTED bytes (pinned at `resource.register`),
  never the live working tree: after fixing a gated artifact (plan, report,
  graph, project_graph, reflection_lens_doc, reflection_doc, change_spec),
  re-register it to submit the
  fix — editing the file alone changes nothing the workflow can see
- do not create artifact manifests or content-addressed resource objects
- do not restore old versions through MCP; edit the live file normally and
  re-register it to submit a new version

## Review discipline

When `workflow.status_and_next` says to launch or wait for a reviewer, follow
`workflow.review_gate`. If no request exists, call `review.request` with the
exact target and role: `target_type: "experiment"`, the experiment's
`target_id`, `role: "design_reviewer" | "experiment_reviewer"`, and your own
`producer_session_id` (plus optional `reason`). Launch a separate reviewer with
the returned `reviewer_handoff` — its `spawn_prompt` is a ready-made prompt for
the reviewer subagent — and `reviewer_capability`. Reflection review instead
uses `target_type: "reflection"` and `role: "reflection_reviewer"` as described
by the `project-reflection` skill.

The capability plaintext is returned only in the `review.request` response, so
retain it long enough to hand off. It is not consumed by `review.start`: it
remains usable to start a reviewer session while that request is still open.
Do not issue a replacement request merely because a session started, because a
new request supersedes the existing one. Request a fresh capability only when
the prior plaintext was lost, expired, superseded, or the target snapshot
changed.

Reviewer agents must be separate and operate read-only by procedure. They call
`review.start` with exactly `review_request_id`, `reviewer_capability`, their
own `caller_session_id`, and optional `declared_agent`, then submit with the
returned `review_session_id`. The start capability and submit session gate the
review protocol, but the server does not authenticate unrelated tool calls as
reviewer calls. Therefore reviewers must not mutate claims, experiments,
resources, sandboxes, or workflow state. Their `caller_session_id` is required
and must never be the producer session's.

After any review submits, call `workflow.status_and_next` again. MCP's
`revision_context`, experiment state, and allowed actions determine the next
step.

## Completion

Before marking an experiment complete:

- resources are registered and associated
- design and experiment reviews are recorded and accepted by MCP
- conclusion is grounded in files or sandbox outputs
- MCP accepts the transition

If MCP rejects a mutation, follow its `next_action` rather than working around it.
