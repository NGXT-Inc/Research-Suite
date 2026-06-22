# Research Plugin

Research Plugin is the MCP/backend package behind Research Suite. It gives
agentic coding clients a shared state machine for machine learning research:
claims, experiments, repo-file resources, review gates, reflection waves, and
sandboxed execution.

Current version: `0.0008`.

The plugin is client-neutral. Claude Code, Codex, Cursor, Gemini CLI, OpenCode,
and other MCP-capable clients use thin adapters over the same backend, MCP
proxy, skills, and reviewer agents.

## System Shape

```text
Agent client
  Claude Code / Codex / Cursor / Gemini CLI / OpenCode
        |
        v
research-plugin-mcp      sibling frontend UI
  stdio MCP proxy              |
        |                      v
        +------------> research-plugin-http
                         HTTP daemon
                            |
              +-------------+-------------+
              |                           |
        project state              sandbox backends
  <repo>/.research_plugin/     Lambda Labs / Modal / fake
```

The stdio MCP proxy is intentionally thin and stateless. The HTTP daemon owns
durable state, workflow transitions, review permissions, resource metadata,
sandbox orchestration, activity logs, and the HTTP API used by the frontend.

Local mode runs the control and data planes together in `research-plugin-http`.
Split/cloud mode runs a hosted `research-plugin-control` plus a local
`research-plugin-daemon` that keeps repo files, private keys, and machine-local
operations on the user's machine.

## Workflows

Experiments move through enforced gates:

```text
planned -> design_review -> ready_to_run -> running -> experiment_review -> complete
              |                                      |
              +-> back to planned                    +-> back to running or planned
```

Project reflections periodically update project-level memory and direction:

```text
reflecting -> synthesizing -> reflection_review -> published
     ^              ^                |
     +--------------+----------------+
       back to fan-out or synthesis
```

Artifacts are regular files in the research repo. The backend records
repo-relative paths and pinned file versions; review gates check the submitted
snapshot, not whatever happens to be on disk later.

## Quick Start: Local Development

Install backend dependencies once:

```bash
cd /path/to/research_plugin
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Start the daemon before opening an agent client:

```bash
./bin/research-plugin-http \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

For backend development with auto-reload:

```bash
python3 scripts/dev_http_reload.py \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

Then open your agent client in the target research repo, enable the plugin, and
start with:

```text
Use Research Plugin. Start with project.current, then workflow.status_and_next.
```

The active research repo gets its own state and activity under:

```text
<research-repo>/.research_plugin/
```

## Credentials

Provider credentials belong to the daemon process, not the MCP proxy and not the
research repo. Prefer a per-user env file:

```bash
mkdir -p ~/.config/research-plugin
cp .env.example ~/.config/research-plugin/.env
chmod 700 ~/.config/research-plugin
chmod 600 ~/.config/research-plugin/.env
```

Common variables include:

```text
RESEARCH_PLUGIN_LAMBDA_API_KEY=...
MODAL_TOKEN_ID=...
MODAL_TOKEN_SECRET=...
HF_TOKEN=...
```

Lambda Labs is the default sandbox backend. Modal and the fake test backend are
also supported through `RESEARCH_PLUGIN_EXECUTION_BACKEND`.

## Client Adapters

This directory ships one canonical plugin content tree:

- `bin/` - launchers for the MCP proxy, local daemon, split daemon, and control plane
- `backend/` - HTTP daemon, services, state stores, workflow gates, and execution backends
- `mcp_server/` - stdio MCP proxy package
- `skills/` - workflow and review skills
- `agents/` - read-only reviewer agents
- `.claude-plugin/`, `.codex-plugin/`, `.cursor-plugin/`, `gemini-extension.json` - client adapters
- `clients/opencode/` - OpenCode installer and wrappers

See [docs/CLIENTS.md](docs/CLIENTS.md) for client-specific install details and
reviewer handoff behavior.

## Tests

From the plugin root:

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

Use the fake sandbox backend for tests or local workflows that should not touch
a cloud provider:

```bash
export RESEARCH_PLUGIN_EXECUTION_BACKEND=fake
```

## Documentation

- [docs/STARTUP_CHEATSHEET.md](docs/STARTUP_CHEATSHEET.md) - local startup flow
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - backend and mode architecture
- [docs/CLIENTS.md](docs/CLIENTS.md) - Claude Code, Codex, Cursor, Gemini CLI, OpenCode
- [docs/MCP_SERVER_CONTRACT.md](docs/MCP_SERVER_CONTRACT.md) - MCP tools and contracts
- [docs/WORKFLOW_AND_REVIEW.md](docs/WORKFLOW_AND_REVIEW.md) - workflow gates and reviews
- [docs/RESOURCE_MODEL.md](docs/RESOURCE_MODEL.md) - repo-file resource model
- [docs/UI_API.md](docs/UI_API.md) - frontend HTTP API
- [deploy/README.md](deploy/README.md) - reference control-plane deploy
