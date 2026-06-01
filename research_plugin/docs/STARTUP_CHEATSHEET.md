# Startup Cheatsheet

This is the local development startup path for using `research-plugin` with a
research repo.

## Process topology (daemon-first)

The plugin now runs as **one long-lived backend daemon** plus a thin stdio MCP
proxy that Codex spawns on demand:

```text
                   ┌───────────────────────────────────────────┐
                   │  research_plugin HTTP daemon              │
                   │  (research-plugin-http --repo $REPO)      │
                   │                                           │
                   │  - SQLite state, activity log             │
                   │  - SandboxService + Modal SSH sandboxes   │
                   │  - SyncEngine + 60 s poller               │
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

The daemon must be running before Codex makes any tool call. The MCP proxy
discovers it via `$REPO/.research_plugin/daemon.json` (written on startup) or
the `RESEARCH_PLUGIN_DAEMON_URL` env var.

Replace this path with the research repo you want to work inside:

```bash
export RESEARCH_REPO=/Users/guraltoo/Documents/dev/proj/research_work/research_system_test_1
export RESEARCH_PLUGIN=/Users/guraltoo/Documents/dev/proj/experiments/Papyrus/research_plugin
```

## One-Time Plugin Setup

Register the local plugin marketplace from the parent Papyrus repo:

```bash
codex plugin marketplace add /Users/guraltoo/Documents/dev/proj/experiments/Papyrus
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

Install or enable `research-plugin` from `Papyrus Local Plugins`.

## Terminal 1: Execution Backend (Modal credentials)

Modal is the default execution backend behind the `sandbox.*` tools. Codex talks
to the MCP proxy, the proxy talks to the daemon, and the daemon talks to Modal.
Only the daemon process needs Modal credentials (the MCP proxy does not).

Put `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` in a git-ignored `research_plugin/.env`
(auto-detected by the daemon), or point at a file elsewhere / export them:

```bash
# research_plugin/.env  (git-ignored, auto-detected)
MODAL_TOKEN_ID=...
MODAL_TOKEN_SECRET=...

# ...or explicitly:
export RESEARCH_PLUGIN_MODAL_ENV_FILE=/path/to/backend/.env
```

For tests without Modal, use the in-memory fake backend:

```bash
export RESEARCH_PLUGIN_EXECUTION_BACKEND=fake
```

## Terminal 2: Backend Daemon (required for Codex *and* UI)

The HTTP daemon owns SQLite state, the activity log, the sandbox execution
backend, and the volume sync engine. Both the UI and the stdio MCP proxy
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

Development mode with auto-reload and live activity printing:

```bash
cd "$RESEARCH_PLUGIN"

python3 scripts/dev_http_reload.py \
  --repo "$RESEARCH_REPO" \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

One-shot mode without auto-reload:

```bash
"$RESEARCH_PLUGIN/bin/research-plugin-http" \
  --repo "$RESEARCH_REPO" \
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
pkill -f 'backend.http_server --host 127.0.0.1 --port 8787'
```

## Terminal 3: Live Activity

This file is the reliable shared live view for both HTTP calls and
Codex-started MCP tool calls:

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

First list existing projects. If there is no appropriate project, create one.
After we select a project_id, call workflow.status_and_next for that project.
Also check sandbox.health so we know whether the configured execution backend is
available.

Use explicit project_id for every project-scoped MCP tool. Treat local repo
files as resources only after syncing/registering them through MCP.
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
Create or select the experiment, write/sync required plan resources, submit the
design for review, launch a read-only design reviewer agent, and submit the
review back to MCP. If approved, request a sandbox and run the experiment over SSH.
After execution, sync changed output files as resources and run full experiment
review before completing the experiment.
```

## What Should Be Running

Minimum for **any** Codex work (the daemon is no longer optional):

- HTTP daemon on `127.0.0.1:8787` started with `--repo "$RESEARCH_REPO"`
- Codex session in the research repo with `research-plugin` enabled

The MCP proxy is started by Codex itself; the daemon's marker file
`$RESEARCH_REPO/.research_plugin/daemon.json` is how the proxy finds the URL.

Additional, for the UI:

- Browser UI pointed at `http://127.0.0.1:8787`

Additional, for the default Modal backend (daemon env only):

- `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` available directly or through
  `RESEARCH_PLUGIN_MODAL_ENV_FILE`

Recommended while debugging:

- `tail -f "$RESEARCH_REPO/.research_plugin/activity.jsonl"`

## State Files

Research state lives in the research repo, not in the plugin source:

```text
$RESEARCH_REPO/.research_plugin/state.sqlite
$RESEARCH_REPO/.research_plugin/activity.jsonl
```

If the UI does not show the same projects as Codex, check `/health` and confirm
the HTTP backend is pointed at the same `RESEARCH_REPO` that Codex is using.
