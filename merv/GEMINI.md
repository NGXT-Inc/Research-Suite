# Merv

This extension exposes one Merv MCP surface backed by two components:

- a local stdio proxy that owns checkout-relative file IO, project links, local
  storage transfer, feed attachments, and sandbox output pulls; and
- a local or hosted brain that owns durable research records, workflow policy,
  reviews, sandbox lifecycle, provider credentials, blobs, and optional heavy
  storage.

The brain never receives the checkout root or reads the checkout directly. The
proxy submits explicit repo-relative metadata and selected evidence bytes.

## Project scope

Call `project(action="current")` first. If the checkout is unlinked, ask which
existing project to use or what name and summary to create, then call
`project(action="connect", ...)`. The proxy stores the folder link locally.

For normal project-scoped tools, `project_id` is hidden context: do not use it to
switch projects. The proxy removes any supplied value and injects the id linked
to the current checkout. Use `project(action="overview")` for the full claim and
experiment history.

## Operating rules

- Treat the brain state returned through MCP as authoritative. Start or resume
  work with `workflow.status_and_next`, and follow its gate, allowed actions,
  missing evidence, and next action.
- Local edits are not research state. Use `resource.register` to observe a file
  and optionally associate its submitted version with a target and role.
- Load `research-workflow` for experiment work and `project-reflection` for a
  five-lens reflection wave.
- Use a sandbox for long or expensive work; lightweight checks may run locally.
  Do not assume a provider. Inspect `sandbox.options` when hardware selection is
  needed, then use the response's provider-shaped fields.
- For quantitative work, use the MLflow context returned by
  `experiment.transition(start_running)` or call `mlflow.context`. Resume the
  plugin-created run, call `mlflow.finalize_run` before submitting results, and
  do not create a file-backed MLflow store for plugin experiments.

## Review boundary

When a gate requests review, call `review.request` and delegate its handoff to a
separate agent using `experiment-design-review`, `experiment-attempt-review`, or
`project-reflection-review`. That reviewer calls `review.start` with its own
`caller_session_id` and submits the verdict through `review.submit`.

The capability is tied to a role and immutable target snapshot. At
`review.start` the brain rejects invalid/expired/superseded capabilities, stale
snapshots, or a declared reviewer session string equal to the declared producer
string. At submission it rechecks that the request is open and the snapshot is
current. Reviewer read-only behavior is an operating rule imposed by the skill;
the system does not authenticate every unrelated tool call as that reviewer.
This is a practical workflow boundary, not cryptographic proof of independence.

## Sandbox loop

The visible sandbox tools are `sandbox.options`, `sandbox.request`, `sandbox.get`,
`sandbox.attach`, `sandbox.terminal`, `sandbox.runs`, `sandbox.pull_outputs`,
`sandbox.extend`, and `sandbox.release`. Project scope is injected by the proxy;
`sandbox.list` and `sandbox.health` are internal/UI tools and are hidden from the
agent catalog.

The caller generates and owns the SSH keypair. Pass only its public key to
`sandbox.request`; caller private-key material never enters brain state. Brain
management/transcript keys are separate operational credentials. A request may
return `provisioning`; poll with `sandbox.get` rather than requesting again. The
response provides SSH facts, and the agent constructs and runs the SSH command.

Use `rp_run <label> -- <command>` for long commands and inspect receipts with
`sandbox.runs`. Pull compact retained outputs with `sandbox.pull_outputs`
(supplying the caller's `key_path`) or upload heavy files with storage tools when
that optional feature is present. `sandbox.extend` is provider-dependent.
`sandbox.release` is two-step: the first call returns a retention checklist;
only re-call with `confirm_retained=true` after everything valuable is retained.
