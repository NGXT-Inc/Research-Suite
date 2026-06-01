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
gates, and reliable ML job execution.

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
activity log, the job execution backend, and the volume sync poller. The MCP proxy is stateless — it forwards `tools/list` and `tools/call`
to the daemon's `/mcp/*` endpoints. Both the browser UI and Codex go through
the same daemon, eliminating the cross-process race the old split-brain setup
had. **Start the daemon before opening Codex.**

- [docs/STARTUP_CHEATSHEET.md](docs/STARTUP_CHEATSHEET.md) - local startup commands for the daemon, Codex, execution jobs, and activity logs
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

Start the HTTP daemon in the research repo **first** — it owns SQLite, jobs,
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

## Execution job engine

The MCP job tools use a backend-neutral execution layer. Modal is the default
execution backend. Codex should call `job.submit`, `job.status`, `job.logs`, and
`workflow.status_and_next`; it should not talk to Modal, Ray, or any other
provider directly.

For Modal, make sure `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` are available to
the **HTTP daemon process** (the MCP proxy does not need them). In a Papyrus
source checkout, the daemon launcher auto-detects `project/backend/.env`. In
any other deployment, point `RESEARCH_PLUGIN_MODAL_ENV_FILE` at the env file
or export the token variables directly before starting the daemon.

Install and start a local Ray head for development:

```bash
python3 -m venv .venv-ray
. .venv-ray/bin/activate
python -m pip install -r requirements-ray.txt
ray start --head --dashboard-host=127.0.0.1 --dashboard-port=8265 --disable-usage-stats --block
```

In another terminal, run the live smoke test:

```bash
PYTHONPATH=. python3 scripts/smoke_ray_jobs.py --force-rest
```

If the Python running the daemon has Ray installed, the backend uses Ray's
SDK client and can upload a local repo as the Ray `working_dir`. If not, it
uses the Ray Jobs REST API and runs the job from an absolute local cwd, which
works for a local Ray head that can see the same filesystem. Override the Ray
API address with `RESEARCH_PLUGIN_RAY_ADDRESS`.

Override to Ray for local development by setting
`RESEARCH_PLUGIN_EXECUTION_BACKEND=ray` in the daemon's environment after
starting a local Ray head.

For Modal, point the daemon process at the environment file that already
contains the Modal token values:

```bash
export RESEARCH_PLUGIN_MODAL_ENV_FILE=/path/to/project/backend/.env
```

The Modal backend mirrors each project's local repo into a per-project Modal
Volume (`research-plugin-<project_id>`) and mounts that volume writable at
`/workspace/repo` inside every sandbox. Jobs run **inside** the volume — there
is no separate workdir copy. The runner calls `volume.commit()` on exit so
writes persist for the next sync.

A `SyncEngine` keeps the volume and the local repo in agreement via a
three-way diff against a baseline stored at
`.research_plugin/modal/sync.sqlite`. It runs:

- on `project.create` (initial volume + baseline registration),
- on `job.submit` (blocking push; submission fails on conflict),
- on `job.status` reaching a terminal state (pull; brings down whatever the
  job committed, including partial outputs from failed/cancelled runs),
- and every 60 s in a background poller (bidirectional, for both projects
  and external edits while no job is running).

`expected_outputs` on `job.submit` is a workflow hint used by the
`result_sync_required` gate, **not** a transfer instruction — the sync engine
moves every file that changed on either side. Conflicts (both sides changed
since the last sync) halt that path until resolved.

Excluded paths: `.git`, `.research_plugin`, `.venv`/`venv`, `__pycache__`,
`*.pyc`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.cache`, `.aws`,
`node_modules`, `.DS_Store`. Successful, failed, and cancelled jobs retain
the Modal sandbox for 10 minutes before best-effort termination so a
follow-up job can reuse the same instance.

Implemented MCP tools:

- `workflow.status_and_next`
- `project.create`, `project.update`, `project.get`
- `claim.create`, `claim.list`
- `experiment.create`, `experiment.list`, `experiment.get_state`, `experiment.transition`
- `resource.register_file`, `resource.observe_file`, `resource.sync_changed_files`, `resource.associate`, `resource.list`, `resource.resolve`, `resource.history`
- `review.request`, `review.start`, `review.submit`, `review.status`
- `job.submit`, `job.status`, `job.logs`, `job.cancel`, `job.list`, `job.health`
