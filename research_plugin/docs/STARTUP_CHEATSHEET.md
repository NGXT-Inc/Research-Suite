# Startup Cheatsheet

This is the local development startup path for using `research-plugin` with a
research repo.

## Process topology (one brain, thick proxy)

The plugin runs as one brain service plus a thick stdio MCP proxy that Codex
spawns on demand. Local deployment is the same topology as hosted deployment;
only the brain URL and storage/auth defaults change.

```text
                   ┌───────────────────────────────────────────┐
                   │  research_plugin localhost brain          │
                   │  (research-plugin-http)                   │
                   │                                           │
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
                                                  │ repo reads, hashes,
                                                  │ validation, rsync pulls
                                                  │ stdio MCP
                                                  │
                                            ┌─────┴──────┐
                                            │   Codex    │
                                            └────────────┘
```

The localhost brain must be running before Codex makes any local tool call. The
MCP proxy resolves its brain URL as `RESEARCH_PLUGIN_CONTROL_URL` env var >
machine config from `research-plugin-client configure` > the hosted default
`https://experiments.rapidreview.io`. For local dev, point it at the localhost
brain first: `research-plugin-client configure --control-url
http://127.0.0.1:8787` (the stdio proxy still does all repo file work either
way).

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
talks to the MCP proxy, the proxy talks to the brain, and the brain talks to
Lambda Cloud. Only the brain process needs provider credentials (the MCP proxy
does not). Caller SSH private keys stay with the proxy/client side; sandbox
requests send a caller public key.

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

## Terminal 2: Localhost Brain (required for local Codex *and* UI)

The localhost brain owns SQLite state, the activity log, the sandbox execution
backend, and sandbox lifecycle bookkeeping. Both the UI and the stdio MCP proxy
talk to it. **Start this before opening Codex for local work.** The MCP proxy
writes a clear "brain not running" error to Codex if you forget.

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

Development mode with auto-reload and live activity printing runs the localhost
brain:

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

All control work — UI requests and Codex MCP control-tool calls — runs through
the same brain process, so terminal-2 stderr and `/api/activity` both see it.
Repo file reads, validation, folder materialization, and light output pulls run
inside the stdio proxy. The JSONL file still works as a cross-tool tail.

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
exists is false, ask me which project id to link or what project name and
summary to create before calling project.connect, unless I already gave you
that information.
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

Minimum for local Codex work (this cheatsheet covers local dev; hosted users run
no localhost brain, only the stdio proxy):

- localhost brain on `127.0.0.1:8787`
- Codex session in the research repo with `research-plugin` enabled

The MCP proxy is started by Codex itself. Shipped MCP configs leave
`RESEARCH_PLUGIN_CONTROL_URL` empty, so the proxy follows the machine config
(`research-plugin-client configure --control-url http://127.0.0.1:8787` for
local dev) and otherwise defaults to the hosted brain
`https://experiments.rapidreview.io`. Set the env var only to force a one-off
URL, e.g. a non-default local port.

Additional, for the UI:

- Browser UI pointed at `http://127.0.0.1:8787`

Additional, for the default Lambda Labs backend (brain env only):

- `LAMBDA_LABS_API_KEY` (or `RESEARCH_PLUGIN_LAMBDA_API_KEY`)
- `RESEARCH_PLUGIN_LAMBDA_REGION`, for example `us-east-1`
- `RESEARCH_PLUGIN_LAMBDA_INSTANCE_TYPE`, for example `gpu_1x_a10`

Additional, for the Thunder Compute backend (brain env only):

- `RESEARCH_PLUGIN_EXECUTION_BACKEND=thunder_compute`
- `THUNDER_COMPUTE_API_KEY` (or `RESEARCH_PLUGIN_THUNDER_API_KEY`)

Additional, for the Modal backend (brain env only):

- `RESEARCH_PLUGIN_EXECUTION_BACKEND=modal`
- `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` available directly or through
  `RESEARCH_PLUGIN_MODAL_ENV_FILE`

Thunder and Lambda provisioning create SSH-ready VMs with the agent shell/ML
tooling baseline. Output handoff is explicit: copy retained light files back
over SSH before release, and use durable storage for heavy artifacts.

Recommended while debugging:

- `tail -f "$RESEARCH_REPO/.research_plugin/activity.jsonl"`

## State Files

In local deployment, brain state is under the local brain state directory
(default `~/.research_plugin/brain`, or the configured registry-derived state
directory in dev/test). Repo-local activity and retained artifacts still live in
the research checkout:

```text
$RESEARCH_REPO/.research_plugin/activity.jsonl
$RESEARCH_REPO/experiments/<name>/
```

The proxy keeps the checkout-to-project link database under the machine config
directory (`~/.research_plugin/project_links.sqlite` by default). If Codex does
not resolve the expected project, check `/health`, `RESEARCH_PLUGIN_CONTROL_URL`,
and the link — fix it in-session with `project.connect` or from a terminal with
`research-plugin-client link --project-id ...`.
