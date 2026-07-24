# Merv

Merv is a plugin for agentic coding platforms that helps agents run machine learning research as gated, reviewable experiment workflows.

It is designed to work with Claude Code, Codex, Cursor, Gemini CLI, OpenCode,
OpenHands, Replit Agent, and other MCP-capable agent platforms. It includes a
frontend for humans to observe agent behavior ranging from macro research
strategy to experiment execution specifics.

The goal is to give research agents enough structure to plan experiments, execute them, review results, and reflect on the project direction to handle open-ended research problems.

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

After a set of experiments is complete, the plugin drives a project-wide reflection. Five different sub-agents are called, each analyzing the wave's snapshot of all terminal experiments and current claim statuses under a different lens. Their goal is to look for patterns of what works, what does not, and what has not been tried, in order to set up the next phase of experiments. The analysis of the sub-agents is consolidated into a report, logic graph, and change spec. Those artifacts are adversarially reviewed by a different agent for accuracy.

## How the system fits together

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/system-architecture-dark.svg">
  <img alt="System architecture: agent platforms connect directly to the brain over HTTP MCP with a project key; the brain owns durable records and workflow gates and provisions cloud sandboxes; agents run SSH commands and pull retained outputs themselves. The frontend supervises the brain." src="assets/system-architecture-light.svg">
</picture>

Merv has three main pieces:

- **Agent adapters** connect Claude Code, Codex, Cursor, Gemini CLI, OpenCode,
  OpenHands, Replit Agent, and other agentic clients to the same workflow.
- **Backend** owns the research state: projects, claims, experiments, artifacts, review gates, reflections, and sandbox orchestration.
- **Frontend** gives humans a visual way to inspect the project: experiments, reviews, artifacts, logic graphs, timelines, and current progress.

By default the plugin connects to the hosted brain; it can also run fully
locally. In either deployment the checkout root and caller SSH private keys
stay on the user's machine. Agents send explicit project ids, typed metadata,
and selected submitted bytes; the brain never opens the checkout directly.
Brain management keys remain separate operational credentials.

## Install

There is no local proxy process and no `pip` install: every client connects
directly to the configured brain's `/mcp` endpoint over HTTP (the hosted brain
by default). The `merv-client` CLI, `merv-http`, and brain run on Python 3.11+.
Sandbox SSH and output-pull workflows additionally use the system OpenSSH
client and `rsync`. For Codex, Gemini CLI, OpenCode, OpenHands, and Replit
Agent, see [merv/docs/CLIENTS.md](merv/docs/CLIENTS.md) and the
[cross-platform matrix](merv/docs/AGENT_ANYWHERE.md).

### Claude Code

```bash
claude plugin marketplace add https://rapidreview.io/marketplace.json
claude plugin install merv@rapidreview
```

Restart Claude Code.

### Cursor

Cursor loads local plugins from a directory. Clone the repo and **copy** the plugin bundle into `~/.cursor/plugins/local` (Cursor rejects symlinks that point outside that directory):

```bash
git clone https://github.com/NGXT-Inc/Merv.git ~/Merv
mkdir -p ~/.cursor/plugins/local
rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude '*.egg-info' \
  ~/Merv/merv/ ~/.cursor/plugins/local/merv/
# Optional, only for merv-client/merv-http when `python3` is older than 3.11:
python3.11 -m venv ~/.cursor/plugins/local/merv/.venv
```

Then enable **merv** on Cursor's Customize page and restart Cursor (or run **Developer: Reload Window**). To update later: `git -C ~/Merv pull`, re-run the `rsync`, and reload.

### Connect with a project key

The hosted brain authenticates each agent client with a **project-scoped key**.
Create the project and mint a key in the UI, then export it:

1. Open [RapidReview](https://rapidreview.io/map), sign in, and open (or
   create) the project this client should be bound to.
2. Create a key for that project and copy it when shown — it is displayed once.
3. Export it where the agent runs:

```bash
export MERV_MCP_KEY=mk_...
```

The committed MCP config reads the key from that env var and never inlines it.
A key is bearer-equivalent to full access to its one bound project, so treat it
like a password. Details:
[HOSTED_CLIENT_QUICKSTART.md](merv/docs/HOSTED_CLIENT_QUICKSTART.md).

### First run

Open the repo you want to research as the workspace, then ask the agent to call
`project(action="current")` — it returns the key's bound project and its id.
The agent then passes that id as `project_id` on project-scoped calls, starting
with `workflow.status_and_next(project_id)`.

## Migrating from Research Suite (`research-plugin`)

Upgrading from the old `research-plugin`? Everything was renamed in v0.0012
and the hosted brain now requires sign-in, but your data carries over
untouched. See [MIGRATING.md](MIGRATING.md) for the per-client steps
(Claude Code, Cursor, Codex).
