# Research Plugin

This extension connects you to the Research Plugin MCP server: a research
kernel that owns durable state (claims, experiments, resources, reviews,
syntheses), the gated experiment workflow, and cloud sandbox provisioning.
A shared HTTP daemon must be running first (see the extension README's
"Use with Gemini CLI" section); the MCP server is a thin proxy to it.

Operating rules:

- Treat the MCP server as the single authority for research and workflow
  state. Never reconstruct workflow state from memory.
- Call `project.current` first. Then call `workflow.status_and_next` before
  acting, and follow its `next_action`, allowed actions, and gate guidance.
- Local file edits are not research state. A file only becomes a research
  resource after `resource.register_file` + `resource.associate`.
- For the full operating procedure, load the `research-workflow` skill. For
  project-level reflection waves, load the `research-reflection` skill.
- When `workflow.status_and_next` asks for a design, experiment, or synthesis
  review, delegate to the matching bundled subagent (`design-review`,
  `experiment-review`, or `synthesis-review`), passing the target id,
  `review_request_id`, and `reviewer_capability` in the prompt. Reviewers are
  read-only and submit verdicts themselves via `review.start` / `review.submit`.
- Expensive or GPU work runs in a sandbox over SSH (`sandbox.request` /
  `sandbox.terminal` / `sandbox.sync` / `sandbox.release`), never locally.
