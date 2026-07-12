# Local Development Startup

This runbook starts a local brain while keeping the same topology used by the
hosted service: an agent-launched stdio MCP proxy performs checkout-local data
work and talks to one brain over HTTP.

```text
Agent client --stdio--> local MCP proxy --HTTP--> localhost brain
                         |                         |
                         +-- checkout IO          +-- SQLite / blobs / providers

Browser UI -------------------------------------> localhost brain
```

Hosted users do not need this runbook. The shipped proxy connects to the hosted
brain by default and still performs all checkout-local work on the user's
machine.

## Prerequisites

- Python 3.11+
- a POSIX shell; OpenSSH and `rsync` when exercising sandbox access/output pulls
- Node.js/npm only when developing the browser UI
- provider credentials only when provisioning real sandboxes

Set convenient paths:

```bash
export RESEARCH_SUITE=/path/to/Merv
export RESEARCH_PLUGIN="$RESEARCH_SUITE/merv"
export RESEARCH_REPO=/path/to/research/repo
```

## Install brain dependencies

The stdio proxy needs no packages, but the brain does:

```bash
cd "$RESEARCH_PLUGIN"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For safe local development without cloud provisioning:

```bash
export RESEARCH_PLUGIN_EXECUTION_BACKEND=fake
```

For a real backend, leave the default `lambda_labs` selection or set
`RESEARCH_PLUGIN_EXECUTION_BACKEND` to `thunder_compute` or `modal`, then provide
the corresponding credentials to the brain process. Caller SSH private keys
remain on the client/proxy side.

## Start the brain

One-shot:

```bash
cd "$RESEARCH_PLUGIN"
./bin/merv-http --host 127.0.0.1 --port 8787
```

Auto-reload while editing backend code:

```bash
cd "$RESEARCH_PLUGIN"
python3 scripts/dev_http_reload.py --host 127.0.0.1 --port 8787
```

Check the process from another terminal:

```bash
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/api/meta
curl -s http://127.0.0.1:8787/api/projects
```

## Point the proxy at localhost

Machine configuration is the normal local-development path:

```bash
"$RESEARCH_PLUGIN/bin/merv-client" configure \
  --control-url http://127.0.0.1:8787
```

The proxy resolves its URL from `RESEARCH_PLUGIN_CONTROL_URL`, then this machine
configuration, then the hosted default. Use the environment variable only for a
one-off override.

## Register the plugin

Use the client-specific instructions in [CLIENTS.md](CLIENTS.md). For example,
when developing the Codex plugin from this checkout, add the repository as a
local marketplace and enable `merv` in the target workspace.

Open the research checkout in the agent client. The first calls should be:

```text
project(action="current")
workflow.status_and_next()
```

If the folder is not linked, use:

```text
project(action="connect", project_id="proj_...")
```

or provide `name` and `summary` to create and link a project in one call. The
link is machine-local; the brain receives only the project id, never the folder
path.

The terminal fallback is:

```bash
cd "$RESEARCH_REPO"
"$RESEARCH_PLUGIN/bin/merv-client" link --project-id proj_...
```

## Start the UI

With the brain running:

```bash
cd "$RESEARCH_SUITE/research_state_ui"
npm install
npm run dev
```

Vite serves `http://127.0.0.1:5173` and proxies `/api` and `/health` to the
brain on port 8787. Point at another local port with `RSUI_API`.

## Observe the system

- `GET /api/activity?limit=100` returns the bounded in-memory activity ring.
- `GET /api/debug/tool-calls` returns the bounded in-memory full-payload
  diagnostic ring.
- `GET /api/projects/{project_id}/events` returns durable accepted research
  events.
- `GET /api/projects/{project_id}/events/stream` is the UI's server-sent-event
  notification stream.

The activity and tool-call diagnostic rings are process-local and reset when the
brain restarts. They are not repo-local JSONL or SQLite files.

## State placement

- The local brain stores SQLite state and submitted blobs under its configured
  brain state root (by default derived from `~/.research_plugin/brain`).
- The proxy stores checkout-to-project links in the machine configuration
  directory, normally `~/.research_plugin/project_links.sqlite`.
- The research checkout stores experiment folders and retained evidence only;
  it does not contain the brain database.

## Optional full hosted-shape stack

To exercise Postgres, MinIO, MLflow, and the control preset locally:

```bash
cd "$RESEARCH_PLUGIN"
docker compose -f deploy/docker-compose.yml up --build
```

This is the reference deployment shape, not a production platform. See
[deploy/README.md](../deploy/README.md) for its security and operational seams.
