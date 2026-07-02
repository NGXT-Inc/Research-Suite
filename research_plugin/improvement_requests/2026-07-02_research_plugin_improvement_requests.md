# Research Plugin Improvement Requests

Date: 2026-07-02

This list comes from hands-on use of the Research Plugin while running the
`autoresearch_alpha` experiment workflow. The Git and Reproducibility section
was intentionally omitted per request.

## Highest Impact

1. **Make sandbox lifecycle more explicit and durable**

   The biggest operational issue was VM state. A retained VM can expire,
   terminate, or become unreachable while an experiment is running, and the
   plugin should surface why: expired, provider preempted, SSH failed, user
   release, bootstrap failure, and so on. Right now `terminated` is not enough
   to decide whether to retry, recover artifacts, or blame the experiment.

2. **Add a first-class project active VM concept**

   Reusing one VM across experiments is possible with `sandbox_attach`, but it
   is still manual. A project-level preferred live sandbox would reduce
   accidental new VM creation and make reuse the default when a researcher wants
   continuity across runs.

3. **Provide automatic lease extension or expiry warnings during active runs**

   If an experiment is `running`, the VM should either auto-extend, warn loudly
   before expiry, or prevent starting a run too close to expiry. Losing a VM
   mid-run wastes the approved design, MLflow setup, and candidate commit.

4. **Persist command status independently of SSH**

   The tmux supervisor is helpful, but when SSH becomes unreachable, command
   state and transcript access can disappear. A plugin-side command record with
   `command_id`, start time, current status, exit code, and last output would
   make recovery much cleaner.

5. **Automatic artifact retention from sandboxes**

   Manual `rsync` is error-prone. The plugin should offer a
   `sandbox_pull_outputs(experiment_id, paths)` helper or default sync for
   `experiments/<name>/results`, `report.md`, and `graph.json`. Resource tools
   only see local files, so retention is a critical workflow step.

## Experiment Workflow

6. **Expose `workflow.status_and_next` consistently**

   The skill documentation says to call it, but it was not exposed in the tool
   set I had. I had to infer next steps from `experiment_get_state`. That works,
   but it weakens the plugin's intended single-source-of-truth workflow model.

7. **Create local experiment folders when experiments materialize**

   Reflection publish created planned experiments in MCP, but the local folders
   were not present. The plugin should create `experiments/<name>/` locally on
   publish or provide a sync command.

8. **Batch resource association**

   Registering many result files is manageable, but associating each resource
   one by one is noisy and easy to get wrong. A batch association API would
   reduce mistakes, for example a payload of `{resource_id, role, target}` rows.

9. **Preflight linter for gated artifacts**

   Plan, report, and graph validation happen at transition time. A
   `resource_validate(path, role)` tool would catch missing headings, oversized
   files, graph cycles, unresolved figures, and role mismatches before
   submission.

10. **Better attempt retry semantics**

    When a VM dies mid-run, the experiment remains `running`, but there is no
    clean "retry current approved attempt because infrastructure failed" path. A
    retry transition or explicit infrastructure-failure rerun state would be
    useful.

11. **Auto-suggest claim updates after reviewed completion**

    After a reviewed negative result, I still had to manually call
    `claim_update`. The plugin has enough information to suggest or apply a
    scoped claim update during `complete`.

## MLflow

12. **Create the MLflow run at experiment start**

    The plugin gives the MLflow experiment URI, which is good. But run identity
    still has to be built in a custom script. A plugin-created run ID, available
    at `start_running`, would make logs, artifacts, and readbacks more
    consistent.

13. **Fix stale immediate MLflow readbacks**

    Some runs read back as `RUNNING` immediately after the script had finished.
    A final REST readback fixed it, but the plugin should provide a canonical
    finalize/readback helper.

14. **Surface MLflow links directly in experiment state**

    The experiment state should show current MLflow experiment and run links
    after `start_running` or result submission. It should not require digging
    through `metrics.json` or wrapper output.

## Storage

15. **Make storage easier to use end-to-end**

    The storage feature worked, but it felt low-level. I would want
    `storage_upload_file(path, kind, experiment_id)` and
    `storage_download(object_id, path)` helpers, not just object registration
    primitives.

16. **Associate storage objects with experiments and reports more visibly**

    Storage objects should show up alongside resources in experiment state. If a
    run log or model artifact is in storage, reviewers should see that directly.

17. **Give clearer guidance on what belongs in storage**

    The split between resources, artifacts, and storage is conceptually right,
    but agents need clearer thresholds: logs over a size limit, checkpoints,
    datasets, generated cache, and similar bulky or durable outputs.

## Reviewer Gates

18. **Reduce review boilerplate**

    The design and attempt review model is strong, but launching a subagent with
    a one-time capability is verbose. A helper like `review_request_and_start`
    would preserve separation while reducing operational overhead.

19. **Show review gate checklist in experiment state**

    `allowed_transitions` gives requirements, but a checklist would be better:
    plan associated, design review passed, result resource present, report
    present, graph valid, experiment review pending.

20. **Reviewer capability recovery**

    If a one-time capability is lost, the current answer is to request another
    review. That is workable, but the plugin should make stale or pending review
    capabilities easier to inspect and refresh.

## Reflection Workflow

21. **Reflection is powerful but heavy**

    The five-lens reflection produced useful decisions, but the orchestration
    cost is high: spawn agents, register lens docs, synthesize, review, and
    publish. The plugin could provide a dashboard of missing lenses/artifacts
    and a guided publish checklist.

22. **Better graph diffing between reflections**

    Project graph updates are a central artifact. A previous-vs-new graph diff
    would help reviewers see what changed, what was pruned, and why.

23. **Materialized experiment visibility after reflection publish**

    Publish created claims and experiments correctly, but the next-step UI
    should make "these are now planned; create local folders / start here" very
    obvious.

## Overall

The core model is good: claims, experiments, resources, reviews, reflection,
MLflow, and storage form a real research operating system. The main
improvements I would prioritize are operational reliability and workflow
friction: durable sandbox runs, automatic artifact retention, tighter MLflow
integration, batch resource workflows, and clearer state transitions when
infrastructure fails.
