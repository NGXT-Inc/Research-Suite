# Research Plugin

This extension connects you to the Research Plugin MCP server: a research
kernel that owns durable state (claims, experiments, resources, reviews,
reflections), the gated experiment workflow, and cloud sandbox provisioning.
The MCP server is a stdio proxy that always dials
`RESEARCH_PLUGIN_CONTROL_URL`. For local deployments that URL is the localhost
brain (`research-plugin-http`, start it first); for hosted deployments it is the
hosted brain. The proxy always performs checkout-local data-plane work itself:
repo reads, hashing, validation, output pulls, and caller SSH key custody.

Operating rules:

- Treat the MCP server as the single authority for research and workflow
  state. Never reconstruct workflow state from memory.
- Call `project` with `action: "current"` first. Then call
  `workflow.status_and_next` before acting, and follow its `next_action`,
  allowed actions, and gate guidance. For the full-project read — every claim
  and experiment including settled/terminal ones — call `project` with
  `action: "overview"`.
- Local file edits are not research state. A file only becomes a research
  resource after `resource.register_file` + `resource.associate`.
- For the full operating procedure, load the `research-workflow` skill. For
  project-level reflection waves, load the `project-reflection` skill.
- When `workflow.status_and_next` asks for a design, experiment, or reflection
  review, delegate to the matching bundled subagent (`experiment-design-review`,
  `experiment-attempt-review`, or `project-reflection-review`), passing the target id,
  `review_request_id`, and `reviewer_capability` in the prompt. Reviewers are
  read-only and submit verdicts themselves via `review.start` / `review.submit`.
- Expensive or GPU work runs in a sandbox over SSH (`sandbox.request` /
  `sandbox.terminal` / `sandbox.release`), never locally. Copy retained files
  off the box explicitly over SSH, or upload heavy artifacts with storage tools,
  before releasing the sandbox.
- For quantitative runs, call `mlflow.context` before training, then set the
  returned `MLFLOW_TRACKING_URI` and `MLFLOW_EXPERIMENT_NAME` in the local or
  SSH command that starts the run. Do not infer tracking from the current shell,
  and do not create a file-backed local MLflow store for plugin experiments.
