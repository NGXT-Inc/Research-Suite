# research_plugin

Lean Codex plug-in architecture for research state.

Current version: `0.0004`.

This project starts fresh. It does not port the existing `research_state_mockup/backend`
implementation. The goal is to keep the durable model small:

- claims: what we think
- experiments: what we try
- resources: regular files in the local repo

Codex owns local reasoning, editing, lightweight scripts, and reviewer-agent
delegation. The backend (an HTTP daemon, fronted to Codex by a stdio MCP
proxy) owns mutation permissions, workflow state, durable memory, review
gates, and Modal sandbox provisioning for ML execution.

## First reduction

The first deliberate simplification is the resource model:

> one repo file maps to one resource.

The server stores a repo-relative file path plus append-only observed versions.
Each version captures size, mtime, content sha256, and mimetype — but not the
file contents. Historical content is whatever the user's repo still has on
disk or in their own git history. The plugin does not need artifact refs,
manifests, previews, or cache directories for the MVP.

Project scope is explicit. The backend supports multiple projects in one local
workspace, but project-scoped tools require `project_id`; there is no fallback
to the first-created project.

See [docs/RESOURCE_MODEL.md](docs/RESOURCE_MODEL.md).

## Architecture

The plugin runs as **one long-lived HTTP daemon** plus a **thin stdio MCP
proxy** that Codex spawns on demand. The daemon owns SQLite state, the
activity log, the sandbox execution backend, and the volume sync poller. The MCP proxy is stateless — it forwards `tools/list` and `tools/call`
to the daemon's `/mcp/*` endpoints. Both the browser UI and Codex go through
the same daemon, eliminating the cross-process race the old split-brain setup
had. **Start the daemon before opening Codex.**

- [docs/STARTUP_CHEATSHEET.md](docs/STARTUP_CHEATSHEET.md) - local startup commands for the daemon, Codex, sandboxes, and activity logs
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - full plug-in architecture
- [docs/MCP_SERVER_CONTRACT.md](docs/MCP_SERVER_CONTRACT.md) - MCP tools and state ownership
- [docs/UI_API.md](docs/UI_API.md) - lightweight HTTP API for frontend work
- [docs/WORKFLOW_AND_REVIEW.md](docs/WORKFLOW_AND_REVIEW.md) - experiment workflow and review gates
- [docs/REVIEW_IDENTITY.md](docs/REVIEW_IDENTITY.md) - local reviewer identity model
- [docs/CLAUDE_FRONTEND_HANDOFF.md](docs/CLAUDE_FRONTEND_HANDOFF.md) - context for rebuilding the UI

## Plugin contents

- `.codex-plugin/plugin.json` - Codex plug-in manifest
- `.mcp.json` - MCP server registration through the local launcher
- `backend/` - HTTP daemon code: services, SQLite state, activity log, execution backends, volume sync (Python package `backend`)
- `mcp_server/` - thin stdio MCP proxy that forwards tool calls to the daemon (Python package `mcp_server`)
- `bin/research-plugin-mcp` - launcher for the stdio MCP proxy
- `bin/research-plugin-http` - launcher for the HTTP daemon
- `skills/research-workflow/SKILL.md` - primary operating skill for Codex
- `skills/design-review/SKILL.md` - read-only design review skill
- `skills/experiment-review/SKILL.md` - read-only full experiment review skill

## v0.0004 server

The server uses Pydantic for tool contracts and FastAPI for the UI-facing HTTP
adapter.

Install core backend dependencies in a plugin-local virtualenv:

```bash
cd /path/to/research_plugin
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

The launchers use `.venv/bin/python` automatically when that virtualenv exists.
Set `RESEARCH_PLUGIN_PYTHON=/path/to/python` to force a different interpreter.

Run tests:

```bash
PYTHONPATH=research_plugin research_plugin/.venv/bin/python -m unittest discover -s research_plugin/tests -v
```

Start the HTTP daemon in the research repo **first** — it owns SQLite, sandboxes,
and sync, and writes `.research_plugin/daemon.json` so the MCP proxy can find
it:

```bash
/path/to/research_plugin/bin/research-plugin-http --repo /path/to/research-repo --host 127.0.0.1 --port 8787
```

Then Codex (or any other caller) can launch the stdio MCP proxy from inside
the research repo — it discovers the daemon URL from the marker file:

```bash
cd /path/to/research-repo
/path/to/research_plugin/bin/research-plugin-mcp
```

Set `RESEARCH_PLUGIN_DAEMON_URL` to override the discovered URL. The MCP
proxy never opens SQLite, spawns sandboxes, or writes activity itself —
those all happen inside the daemon.

Run the HTTP API with auto-reload during backend development:

```bash
python3 scripts/dev_http_reload.py \
  --repo /path/to/research-repo \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

The reload helper watches the plugin backend source but stores project state in
the `--repo` research workspace. If port `8787` is already occupied, stop the
existing HTTP process or use another port.

Activity is also appended to:

```text
/path/to/research-repo/.research_plugin/activity.jsonl
```

Use that file to watch both HTTP API activity and Codex-started MCP tool calls:

```bash
tail -f /path/to/research-repo/.research_plugin/activity.jsonl
```

All activity (UI calls and Codex MCP tool calls) flows through the same daemon
process, so terminal-side `--activity-stderr` and `/api/activity` both see
everything. The JSONL file still works as a cross-tool tail.

For local Codex plugin development, register the parent repo marketplace:

```bash
codex plugin marketplace add /Users/guraltoo/Documents/dev/proj/experiments/Papyrus
```

```text
.agents/plugins/marketplace.json
```

It points to `./research_plugin`. After installation, plugin state is stored in
the active research repo at `.research_plugin/state.sqlite`, not beside the
plugin code.

## Local shipping

`research-plugin` is designed to be installed once and used from arbitrary
research repos:

```text
installed plugin code
  /path/to/research_plugin

target research repo
  /path/to/my-ml-project
  .research_plugin/state.sqlite
  local files used as resources
```

The MCP launcher resolves its own install directory, adds
`<plugin_dir>` to `PYTHONPATH` (so the `mcp_server` package is importable),
and defaults the repo for daemon discovery to the current working directory:

```text
RESEARCH_PLUGIN_REPO_ROOT=$PWD
```

The MCP proxy uses that to locate `.research_plugin/daemon.json`. Set
`RESEARCH_PLUGIN_DAEMON_URL` to override discovery entirely. State paths
(`RESEARCH_PLUGIN_STORE`) are only consumed by the HTTP daemon — pass them
to `research-plugin-http`, not to the MCP launcher.

```bash
/path/to/research_plugin/bin/research-plugin-http --repo /path/to/my-ml-project --port 8787
```

For this local marketplace install, `.mcp.json` uses the absolute path to
`bin/research-plugin-mcp`. Codex starts MCP from the active research repo, so a
relative `./bin/...` command would incorrectly point into that repo.

The MCP config also sets `default_tools_approval_mode` to `approve` for this
local plugin. That is a Codex client approval setting, not a backend permission
check. The backend still enforces its own workflow and reviewer permissions, and
Codex/user config can override tool approval behavior.

Fresh Codex session smoke checklist:

1. Open a research repo that does not contain the plugin source.
2. Use `/plugins` and enable `research-plugin` from `Papyrus Local Plugins`.
3. Ask Codex to use the research workflow skill.
4. Confirm `workflow.status_and_next` works with an explicit `project_id`.
5. Create a claim and experiment.
6. Write a local plan/result file in the research repo.
7. Register the file through MCP as a resource.
8. Confirm `.research_plugin/state.sqlite` exists in the research repo, not in
   the plugin install directory.
9. Run design and experiment review.
10. Confirm stale resources/reviews do not satisfy later attempts.

## Sandbox execution engine

There is no job abstraction. The agent **requests a sandbox** for an experiment
and runs shell commands on it directly over SSH. Modal is the default backend.
The agent calls `sandbox.request` / `sandbox.get` / `sandbox.terminal` /
`sandbox.release`; it never talks to Modal directly.

Provisioning is **best-effort-synchronous**: creating a sandbox (large first
sync, cold GPU) can outlast the MCP call timeout, so `sandbox.request`
provisions on a background thread and waits up to a budget (default 45s,
`RESEARCH_PLUGIN_SANDBOX_REQUEST_WAIT`). If it comes up in time you get
`status: running` with `ssh.command` inline; otherwise you get
`status: provisioning` and **poll `sandbox.get`** (read-only) until it is
`running` or `failed`. `get` reconciles a provisioning row whose job died
(daemon restart) to `failed`, so a poll loop always terminates; the sandbox id
is persisted the instant the sandbox is created and a partial failure terminates
it, so a timed-out or canceled request never orphans a Modal sandbox.

For Modal, make sure `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` are available to
the **HTTP daemon process** (the MCP proxy does not need them). The simplest way
is a git-ignored `.env` at the plugin root — `research_plugin/.env` — which the
daemon auto-detects:

```bash
# research_plugin/.env  (git-ignored)
MODAL_TOKEN_ID=...
MODAL_TOKEN_SECRET=...
```

Resolution order: `RESEARCH_PLUGIN_MODAL_ENV_FILE` (if set) → `research_plugin/.env`
→ variables already exported in the environment (which always win). So you can
also point at a file elsewhere or export the tokens directly:

```bash
export RESEARCH_PLUGIN_MODAL_ENV_FILE=/path/to/backend/.env
```

`SandboxService` is the central registry: **one sandbox per experiment**,
reuse-if-alive-else-create. On `sandbox.request` it generates a per-experiment
ed25519 keypair, creates a Modal sandbox with the project Volume mounted and
`openssh-server` running, exposes SSH over an unencrypted Modal tunnel
(`unencrypted_ports=[22]`), authorizes the public key, and returns SSH details.

To keep agent commands short, the registry also drops a static dispatcher at
`.research_plugin/sbx` and a per-experiment connection file under
`.research_plugin/sandboxes/conn/<experiment_id>` (regenerated each request,
since the host/port change). The agent runs
`.research_plugin/sbx <experiment_id> '<command>'` instead of a ~210-character
`ssh` line; the response's `ssh.command` is that short form and `ssh.raw_command`
is the full `ssh` invocation for use outside the repo root. Releasing or expiring
a sandbox removes the conn file so the dispatcher fails loudly rather than
connecting to a recycled host:port.

Visibility: an in-sandbox `sshd` `ForceCommand` wrapper records every command and
its output to `.research_plugin_sessions/<experiment>/transcript.log` on the
mounted Volume. `sandbox.terminal` reads it (live from the sandbox, falling back
to the committed Volume); the UI renders it as a per-experiment terminal window.

The Modal backend mirrors each project's local repo into a per-project Modal
Volume (`research-plugin-<project_id>`) and mounts it writable at
`/workspace/repo`. A `SyncEngine` keeps the Volume and the local repo in
agreement via a three-way diff against a baseline stored at
`.research_plugin/modal/sync.sqlite`. It runs:

- on `project.create` (initial volume + baseline registration),
- on `sandbox.request` (push current repo before the sandbox boots),
- and every 60 s in a background poller (bidirectional, pulling sandbox writes
  back to the local repo).

Conflicts (both sides changed since the last sync) are recorded and halt the
next `sandbox.request` until resolved.

Excluded paths: `.git`, `.research_plugin`, `.research_plugin_sessions`,
`.venv`/`venv`, `__pycache__`, `*.pyc`, `.mypy_cache`, `.pytest_cache`,
`.ruff_cache`, `.cache`, `.aws`, `node_modules`, `.DS_Store`, `data/raw`,
`data/processed`.

Implemented MCP tools:

- `workflow.status_and_next`
- `project.create`, `project.update`, `project.get`
- `claim.create`, `claim.list`
- `experiment.create`, `experiment.list`, `experiment.get_state`, `experiment.transition`
- `resource.register_file`, `resource.observe_file`, `resource.sync_changed_files`, `resource.associate`, `resource.list`, `resource.resolve`, `resource.history`
- `review.request`, `review.start`, `review.submit`, `review.status`
- `sandbox.request`, `sandbox.get`, `sandbox.list`, `sandbox.release`, `sandbox.terminal`, `sandbox.health`
