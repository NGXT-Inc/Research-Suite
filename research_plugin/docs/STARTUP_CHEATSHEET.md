# Startup Cheatsheet

This is the local development startup path for using `research-plugin` with a
research repo.

## Process topology (daemon-first)

The plugin now runs as **one long-lived backend daemon** plus a thin stdio MCP
proxy that Codex spawns on demand:

```text
                   ┌───────────────────────────────────────────┐
                   │  research_plugin HTTP daemon              │
                   │  (research-plugin-http)                   │
                   │                                           │
                   │  - Project directory router               │
                   │  - SQLite state, activity logs            │
                   │  - SandboxService + Modal/Lambda SSH      │
                   │  - sandbox registry, keys, reapers        │
                   │  - HTTP API at  /api/* and /mcp/*         │
                   └────────────▲──────────────▲───────────────┘
                                │ /api/*       │ /mcp/call
                                │              │
                          ┌─────┴─────┐  ┌─────┴─────────────────┐
                          │ Browser UI│  │ MCP stdio proxy       │
                          │ (Vite)    │  │ (research-plugin-mcp) │
                          └───────────┘  └────────▲──────────────┘
                                                  │ stdio MCP
                                                  │
                                            ┌─────┴──────┐
                                            │   Codex    │
                                            └────────────┘
```

The daemon must be running before Codex makes any tool call. The MCP proxy tries
`RESEARCH_PLUGIN_DAEMON_URL`, then `$REPO/.research_plugin/daemon.json`, then
the default shared URL `http://127.0.0.1:8787`. A fresh folder can therefore
call `project.current` before it has a marker, as long as the shared daemon is
running on the default port.

Replace this path with the research repo you want to work inside:

```bash
export RESEARCH_REPO=/path/to/research-repo
export RESEARCH_PLUGIN=/path/to/research-suite/research_plugin
```

## One-Time Plugin Setup

Register the local plugin marketplace from the parent repo. Marketplace cache
files at the repo root are local development state and are not tracked.

```bash
codex plugin marketplace add /path/to/research-suite
```

Then open Codex in the target research repo:

```bash
cd "$RESEARCH_REPO"
codex
```

Inside Codex:

```text
/plugins
```

Install or enable `research-plugin` from the configured local marketplace.

## Terminal 1: Execution Backend (Lambda Labs credentials)

Lambda Labs is the default execution backend behind the `sandbox.*` tools. Codex
talks to the MCP proxy, the proxy talks to the daemon, and the daemon talks to
Lambda Cloud. Only the daemon process needs provider credentials (the MCP proxy
does not).

Put `LAMBDA_LABS_API_KEY` / `RESEARCH_PLUGIN_LAMBDA_API_KEY` in a git-ignored
env file, or point at a file elsewhere / export it:

```bash
# ~/.config/research-plugin/.env
RESEARCH_PLUGIN_LAMBDA_API_KEY=...

# ...or explicitly:
export RESEARCH_PLUGIN_LAMBDA_ENV_FILE=/path/to/backend/.env
```

For tests without provider credentials, use the in-memory fake backend:

```bash
export RESEARCH_PLUGIN_EXECUTION_BACKEND=fake
```

## Terminal 2: Backend Daemon (required for Codex *and* UI)

The HTTP daemon owns SQLite state, the activity log, the sandbox execution
backend, and sandbox lifecycle bookkeeping. Both the UI and the stdio MCP proxy
forward through it. **Start this before opening Codex.** The MCP proxy writes
a clear "daemon not running" error to Codex if you forget.

Install core backend dependencies once in a plugin-local virtualenv:

```bash
cd "$RESEARCH_PLUGIN"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

The `bin/research-plugin-mcp`, `bin/research-plugin-http`, and reload helper
use `$RESEARCH_PLUGIN/.venv/bin/python` automatically when it exists.
Set `RESEARCH_PLUGIN_PYTHON=/path/to/python` to force a different interpreter.

Development mode with auto-reload and live activity printing runs the shared
multi-project backend:

```bash
cd "$RESEARCH_PLUGIN"

python3 scripts/dev_http_reload.py \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

One-shot mode without auto-reload:

```bash
"$RESEARCH_PLUGIN/bin/research-plugin-http" \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

Health check:

```bash
curl -s http://127.0.0.1:8787/health
```

Project list:

```bash
curl -s http://127.0.0.1:8787/api/projects
```

If port `8787` is already occupied:

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

Stop old research-plugin HTTP/reload processes:

```bash
pkill -f 'scripts/dev_http_reload.py.*--port 8787'
pkill -f 'backend.transport.http_server --host 127.0.0.1 --port 8787'
```

## Terminal 3: Live Activity

Each directory-backed project has its own activity file. After registering or
creating the project for `$RESEARCH_REPO`, this file is the reliable live view
for both HTTP calls and Codex-started MCP tool calls in that project:

```bash
tail -f "$RESEARCH_REPO/.research_plugin/activity.jsonl"
```

Recent activity through HTTP:

```bash
curl -s 'http://127.0.0.1:8787/api/activity?limit=50'
```

All work — UI requests and Codex MCP tool calls — runs through the same daemon
process now, so terminal-2 stderr and `/api/activity` both see everything.
The JSONL file still works as a cross-tool tail.

## Terminal 4: Optional Frontend

Start the frontend from its own project folder and point it at:

```text
http://127.0.0.1:8787
```

The UI should use project-routed endpoints and should not infer an active
project. The user or UI must select a `project_id`.

## Codex Startup Prompt

Open Codex in the target research repo, enable the plugin through `/plugins`,
then start with a prompt like this:

```text
Use Research Plugin.

First call project.current. In project-local MCP it returns the project for the
current folder, or exists:false if the folder does not have a project yet. If
exists is false, ask me what project name and summary to use before calling
project.create, unless I already gave you that information.
Then call workflow.status_and_next.
Also check sandbox.health so we know whether the configured execution backend is
available.

In project-local MCP, the proxy supplies project scope from the current repo.
Treat local repo files as resources only after registering and associating them
through MCP.
```

For an existing project:

```text
Use Research Plugin with project_id proj_...
Call workflow.status_and_next and tell me the current gate, allowed actions,
missing evidence, and the next recommended step.
```

For an experiment run:

```text
Use Research Plugin with project_id proj_...
Create or select the experiment, write and associate required plan resources,
submit the design for review, launch a read-only design reviewer agent, and
submit the review back to MCP. If approved, request a sandbox and run the
experiment over SSH. After execution, copy retained sandbox outputs back to the
local checkout, register/associate them as resources, and run full experiment
review before completing the experiment.
```

## What Should Be Running

Minimum for **any** Codex work (the daemon is no longer optional):

- HTTP daemon on `127.0.0.1:8787`
- Codex session in the research repo with `research-plugin` enabled

The MCP proxy is started by Codex itself. The marketplace MCP config points it
at `http://127.0.0.1:8787` by default. Use `RESEARCH_PLUGIN_DAEMON_URL` only
when the shared daemon is on another host or port. After registration the daemon
also writes `$RESEARCH_REPO/.research_plugin/daemon.json` for discovery.

Additional, for the UI:

- Browser UI pointed at `http://127.0.0.1:8787`

Additional, for the default Lambda Labs backend (daemon env only):

- `LAMBDA_LABS_API_KEY` (or `RESEARCH_PLUGIN_LAMBDA_API_KEY`)
- `RESEARCH_PLUGIN_LAMBDA_REGION`, for example `us-east-1`
- `RESEARCH_PLUGIN_LAMBDA_INSTANCE_TYPE`, for example `gpu_1x_a10`

Additional, for the Thunder Compute backend (daemon env only):

- `RESEARCH_PLUGIN_EXECUTION_BACKEND=thunder_compute`
- `THUNDER_COMPUTE_API_KEY` (or `RESEARCH_PLUGIN_THUNDER_API_KEY`)

Additional, for the Modal backend (daemon env only):

- `RESEARCH_PLUGIN_EXECUTION_BACKEND=modal`
- `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` available directly or through
  `RESEARCH_PLUGIN_MODAL_ENV_FILE`

Thunder and Lambda provisioning create SSH-ready VMs with the agent shell/ML
tooling baseline. Output handoff is explicit: copy retained light files back
over SSH before release, and use durable storage for heavy artifacts.

Recommended while debugging:

- `tail -f "$RESEARCH_REPO/.research_plugin/activity.jsonl"`

## State Files

Research state lives in each project directory, not in the plugin source:

```text
$RESEARCH_REPO/.research_plugin/state.sqlite
$RESEARCH_REPO/.research_plugin/activity.jsonl
```

The shared daemon also keeps a small global registry mapping project ids to
directories (`RESEARCH_PLUGIN_REGISTRY_STORE`, default
`~/.research_plugin/registry.sqlite`). If the UI does not show the same project
as Codex, check `/health`, `RESEARCH_PLUGIN_DAEMON_URL`, and the project's
`repo_root` in `GET /api/projects`.
