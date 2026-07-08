# Research Suite

![Research Suite experiment workflow](experiments.png)

Research Suite is a plugin for agentic coding platforms that helps agents run machine learning research as gated, reviewable experiment workflows.

It is designed to work with Claude Code, Codex, Cursor, Gemini CLI, OpenCode, and other MCP-capable agent platforms. It includes a frontend for humans to observe agent behavior ranging from macro research strategy to experiment execution specifics.

The goal is to give research agents enough structure to plan experiments, execute them, review results, and reflect on the project direction to handle open-ended research problems.

## Quickstart (Claude Code)

Prerequisites: [Claude Code](https://claude.com/claude-code) and `python3` 3.11+ on your `PATH`. That's all — no `pip` install and no local backend; the plugin talks to the hosted control plane by default.

```bash
claude plugin marketplace add NGXT-Inc/Research-Suite
claude plugin install research-plugin@research-suite
```

Restart Claude Code. Then open a session in the repo you want to do research in and ask the agent to run `workflow.status_and_next` — it will orient itself and link the folder to a project.

Setting up a different client (Cursor, Codex, Gemini CLI, OpenCode)? See [research_plugin/docs/CLIENTS.md](research_plugin/docs/CLIENTS.md).

## Experiment-level workflow

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/experiment-workflow-dark.svg">
  <img alt="Experiment workflow: Plan, Design review, Execute, Results review, Complete. Rejected reviews send work back to Execute or Plan." src="assets/experiment-workflow-light.svg">
</picture>

Each experiment begins with a generated plan that is adversarially reviewed by another agent. The plan/review loop persists until the reviewer approves the plan. After approval, the agent proceeds to execution. When it is done, it submits a report that is adversarially reviewed by a different agent. The reviewer can send the agent back to execution to fix something in the execution or the report, or it can send it back to the planning stage if the experiment proved faulty.

## Project-level workflow

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/project-workflow-dark.svg">
  <img alt="Project workflow: completed experiments fan out to five reflection lenses, then Synthesis, Reflection review, Publish. Rejected reviews send work back to Synthesis or the fan-out." src="assets/project-workflow-light.svg">
</picture>

After a set of experiments is complete, the plugin drives a project-wide reflection. Five different sub-agents are called, each analyzing the progress of the last N experiments and the project so far under a different lens. Their goal is to look for patterns of what works, what does not, and what has not been tried, in order to set up the next phase of experiments. The analysis of the sub-agents is consolidated into a report, logic graph, and change spec. Those artifacts are adversarially reviewed by a different agent for accuracy.

## How the system fits together

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/system-architecture-dark.svg">
  <img alt="System architecture: on your machine, agent platforms talk to an MCP proxy that reads the research repo; the proxy sends the hosted control plane project ids, never file paths. The brain owns all research state, the frontend UI reads it, and GPU sandboxes are provisioned by the brain and reached over SSH." src="assets/system-architecture-light.svg">
</picture>

Research Suite has three main pieces:

- **Agent adapters** connect Claude Code, Codex, Cursor, Gemini CLI, OpenCode, and other agentic clients to the same workflow.
- **Backend** owns the research state: projects, claims, experiments, resources, review gates, reflections, and sandbox orchestration.
- **Frontend** gives humans a visual way to inspect the project: experiments, reviews, artifacts, logic graphs, timelines, and current progress.

By default the plugin connects to the hosted control plane; it can also run fully locally. Either way, repo files, SSH keys, and folder-to-project links stay on the user's machine — the control plane sees project ids, never file paths.
