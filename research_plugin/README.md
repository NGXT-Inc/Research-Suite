# research_plugin

Lean Codex plug-in architecture for research state.

Current version: `0.0005`.

This project starts fresh. It does not port the existing `research_state_mockup/backend`
implementation. The goal is to keep the durable model small:

- claims: what we think
- experiments: what we try
- resources: regular files in the local repo

Codex owns local reasoning, editing, lightweight scripts, and reviewer-agent
delegation. The backend (an HTTP daemon, fronted to Codex by a stdio MCP
proxy) owns mutation permissions, workflow state, durable memory, review
gates, and cloud sandbox provisioning (Lambda Labs by default, Modal optional)
for ML execution.

## First reduction

The first deliberate simplification is the resource model:

> one repo file maps to one resource.

The server stores a repo-relative file path plus append-only observed versions.
Each version captures size, mtime, content sha256, and mimetype — but not the
file contents. Historical content is whatever the user's repo still has on
disk or in their own git history. The plugin does not need artifact refs,
manifests, previews, or cache directories for the MVP.

Project scope is directory-backed. The shared backend can serve many projects,
but each MCP proxy is started inside one project folder and forwards that repo
root as hidden context. Agents should call `project.current` first; project-
scoped MCP schemas hide `project_id` when the folder supplies it.

See [docs/RESOURCE_MODEL.md](docs/RESOURCE_MODEL.md).

## Architecture

The plugin runs as **one long-lived HTTP daemon** plus a **thin stdio MCP
proxy** that Codex spawns on demand. The daemon owns SQLite state, the
activity log, the sandbox execution backend, and the SSH rsync poller. The MCP proxy is stateless — it forwards `tools/list` and `tools/call`
to the daemon's `/mcp/*` endpoints. Both the browser UI and Codex go through
the same daemon, eliminating the cross-process race the old split-brain setup
had. **Start the daemon before opening Codex.**

- [docs/STARTUP_CHEATSHEET.md](docs/STARTUP_CHEATSHEET.md) - local startup commands for the daemon, Codex, sandboxes, and activity logs
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - full plug-in architecture
- [docs/CLIENTS.md](docs/CLIENTS.md) - client support matrix and per-client install (Claude Code, Codex, Cursor, Gemini CLI, OpenCode)
- [docs/MCP_SERVER_CONTRACT.md](docs/MCP_SERVER_CONTRACT.md) - MCP tools and state ownership
- [docs/UI_API.md](docs/UI_API.md) - lightweight HTTP API for frontend work
- [docs/WORKFLOW_AND_REVIEW.md](docs/WORKFLOW_AND_REVIEW.md) - experiment workflow and review gates
- [docs/REVIEW_IDENTITY.md](docs/REVIEW_IDENTITY.md) - local reviewer identity model
- [docs/CLAUDE_FRONTEND_HANDOFF.md](docs/CLAUDE_FRONTEND_HANDOFF.md) - context for rebuilding the UI

## Plugin contents

- `.codex-plugin/plugin.json` - Codex plug-in manifest (references `.mcp.codex.json`)
- `.claude-plugin/plugin.json` - Claude Code plug-in manifest
- `.cursor-plugin/plugin.json` - Cursor plug-in manifest (skills/agents/`mcp.json` auto-discovered)
- `gemini-extension.json` - Gemini CLI extension manifest (bundles the MCP server and `GEMINI.md` context)
- `clients/opencode/` - OpenCode adapter: installer, reviewer agents, and config example
- `.mcp.json` - Claude Code MCP server registration, portable via `${CLAUDE_PLUGIN_ROOT}`
- `.mcp.codex.json` - Codex MCP server registration with absolute install path
- `mcp.json` - Cursor MCP server registration (`${workspaceFolder}` supplies the project root)
- `.env.example` - template for the per-user credentials file (see "Use with Claude Code" below)
- `pyproject.toml` - package metadata, dependency declaration, `console_scripts`
- `backend/` - HTTP daemon code: services, SQLite state, activity log, execution backends, and SSH rsync (Python package `backend`)
- `mcp_server/` - thin stdio MCP proxy that forwards tool calls to the daemon (Python package `mcp_server`)
- `bin/research-plugin-mcp` - launcher for the stdio MCP proxy
- `bin/research-plugin-http` - launcher for the HTTP daemon
- `skills/research-workflow/SKILL.md` - primary operating skill (Codex + Claude Code)
- `skills/design-review/SKILL.md` - read-only design review skill (Codex spawn path)
- `skills/experiment-review/SKILL.md` - read-only full experiment review skill (Codex spawn path)
- `agents/design-review.md` - Claude Code subagent for read-only design review (`research-plugin:design-review`)
- `agents/experiment-review.md` - Claude Code subagent for read-only experiment review (`research-plugin:experiment-review`)

## v0.0005 server

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

Start the shared HTTP daemon **first**. It owns the long-lived process and routes
each project to that project's local directory:

```bash
/path/to/research_plugin/bin/research-plugin-http --host 127.0.0.1 --port 8787
```

The legacy single-repo mode is still available with
`--repo /path/to/research-repo`. In shared mode, the UI creates a project by
providing a directory; that directory owns its own `.research_plugin/state.sqlite`,
sync state, sandbox keys, and files. The backend stores only a small global
registry that maps project ids to directories. The mapping is one-to-one: one
project per directory, and one directory per project.

Then Codex (or any other caller) can launch the stdio MCP proxy from inside
the research repo. The MCP proxy stays project-local: it forwards the repo root
as hidden context to the shared daemon, so the agent does not see extra routing
fields or a larger tool schema.
Through MCP, agents should call `project.current`. It returns the folder's
project or `exists: false` with a hint to call `project.create`. It does not
expose or create projects from other folders. The older `project.list` tool is
kept for HTTP/internal compatibility but is not advertised to MCP agents.

```bash
cd /path/to/research-repo
/path/to/research_plugin/bin/research-plugin-mcp
```

The marketplace MCP config points the project-local proxy at
`http://127.0.0.1:8787` by default, so a fresh folder can call `project.current`
before it has a marker. Set `RESEARCH_PLUGIN_DAEMON_URL` only when the shared
daemon is on another host or port. Once a directory-backed project has been
registered, the daemon also writes that directory's `.research_plugin/daemon.json`
marker for discovery. The MCP proxy never opens SQLite, spawns sandboxes, or
writes activity itself — those all happen inside the daemon.

Run the HTTP API with auto-reload during backend development:

```bash
python3 scripts/dev_http_reload.py \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

The reload helper watches the plugin backend source and starts the same shared
multi-project backend as `research-plugin-http`. If port `8787` is already
occupied, stop the existing HTTP process or use another port. Pass `--repo
/path/to/research-repo` only for the legacy single-repo backend.

Activity is also appended to:

```text
<project-dir>/.research_plugin/activity.jsonl
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

### Use with Claude Code

The plugin ships as a Claude Code marketplace plugin. The marketplace lives at
the repo root (`../.claude-plugin/marketplace.json`); the plugin manifest lives
at `.claude-plugin/plugin.json` inside this directory. Skills, subagents, and
the MCP server config (`.mcp.json` with `${CLAUDE_PLUGIN_ROOT}` path placeholder)
all sit at plugin root and are auto-discovered by Claude Code.

**End-user install** (inside Claude Code):

```text
/plugin marketplace add <git-host>/research-suite
/plugin install research-plugin@research-suite
```

For an in-tree local install during development:

```bash
claude --plugin-dir /path/to/research_plugin
```

The MCP proxy that Claude Code spawns ([bin/research-plugin-mcp](bin/research-plugin-mcp))
is **stdlib-only** — it does not need a venv or any Python dependencies, so
there is no install step on the Claude Code path. The HTTP daemon
([bin/research-plugin-http](bin/research-plugin-http)) is what actually needs
`requirements.txt`, and the user starts the daemon manually once per machine
(see below).

Once installed, drop your Modal / Hugging Face credentials at a per-user
location **outside the plugin tree** (the plugin source must never contain
real secrets — see "Credentials" below), then run the daemon once per machine:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/research-plugin-http &
```

**Credentials.** [bin/research-plugin-http](bin/research-plugin-http) resolves
the env file in this priority, first hit wins:

1. `$RESEARCH_PLUGIN_MODAL_ENV_FILE` (explicit deployment override)
2. `${CLAUDE_PLUGIN_DATA}/.env`
3. `${XDG_CONFIG_HOME:-$HOME/.config}/research-plugin/.env`  ← **recommended**
4. `$HOME/.research_plugin/.env`
5. `$PLUGIN_DIR/.env` (dev-only, gitignored)

If none exist, the Modal SDK falls back to its native `~/.modal.toml`. To set
up the recommended location:

```bash
mkdir -p ~/.config/research-plugin
cp /path/to/research_plugin/.env.example ~/.config/research-plugin/.env
chmod 700 ~/.config/research-plugin
chmod 600 ~/.config/research-plugin/.env
# then fill in MODAL_TOKEN_ID, MODAL_TOKEN_SECRET, HF_TOKEN
```

Why this layout matters: `claude plugin install` (with a `directory`-source
marketplace) copies the entire plugin source into
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, including any
files at the source root. A `.env` left in the plugin tree would be copied
into every install. The launcher's user-config lookup means real secrets stay
in `~/.config/research-plugin/` and never enter the plugin tree at all. The
shipped `.env.example` documents which keys are required, with empty values.

Then open any research project — Claude Code spawns the stdio MCP server with
`$PWD` set to the project root, so `RESEARCH_PLUGIN_REPO_ROOT` resolves
correctly and the shared daemon routes to that project's
`.research_plugin/state.sqlite`.

**Approval**: the `default_tools_approval_mode` field is Codex-only and is
absent from the Claude Code `.mcp.json`. Configure approval through
`.claude/settings.json` (allowlist `mcp__research-plugin__*`) or accept the
in-session `/permissions` prompts.

**Reviewer handoff**: when `workflow.status_and_next` returns
`launch_design_reviewer` or `launch_experiment_reviewer`, the orchestrator
calls the Agent tool with `subagent_type` set to
`research-plugin:design-review` or `research-plugin:experiment-review` and
passes `experiment_id`, `review_request_id`, and `reviewer_capability` in the
prompt. The subagent calls `review.start` with the capability, then
`review.submit` with the structured verdict. The skill name returned by the
daemon (`design-review` / `experiment-review`) matches the subagent file name;
the `research-plugin:` namespace prefix is added by Claude Code's plugin
loader.

The Codex install path is unchanged. Its absolute-path `.mcp.codex.json` lives
beside the Claude Code `.mcp.json`; `.codex-plugin/plugin.json` references the
Codex-specific file so both clients coexist without stepping on each other.

### Use with Cursor, Gemini CLI, or OpenCode

The same content tree (`bin/`, `skills/`, `agents/`) is exposed to three more
clients through thin adapters — a Cursor plugin bundle
(`.cursor-plugin/plugin.json` + root `mcp.json`), a Gemini CLI extension
(`gemini-extension.json` + `GEMINI.md`), and an OpenCode installer
(`clients/opencode/install.sh`). The HTTP daemon and startup flow are
identical everywhere; only MCP registration and reviewer-agent spawning are
client-specific. See [docs/CLIENTS.md](docs/CLIENTS.md) for the support
matrix, install commands, and per-client caveats.

#### Updating after source changes

The plugin cache is a snapshot taken at install time, so edits to the source
do not appear live in Claude Code. Refresh both the marketplace metadata and
the plugin snapshot with:

```bash
claude plugin marketplace update research-suite
claude plugin uninstall research-plugin@research-suite && claude plugin install research-plugin@research-suite
```

`claude plugin update research-plugin@research-suite` only re-runs when the
declared `version` in [.claude-plugin/plugin.json](.claude-plugin/plugin.json)
changes. While iterating on the same version, the uninstall + reinstall pair
above is the clean re-snapshot. No venv work runs on session start — the MCP
proxy is stdlib-only.

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
(`RESEARCH_PLUGIN_STORE` and `RESEARCH_PLUGIN_REGISTRY_STORE`) are only consumed
by the HTTP daemon — pass them to `research-plugin-http`, not to the MCP
launcher.

```bash
/path/to/research_plugin/bin/research-plugin-http --port 8787
```

For this local marketplace install, `.mcp.codex.json` uses the absolute path
to `bin/research-plugin-mcp`. Codex starts MCP from the active research repo,
so a relative `./bin/...` command would incorrectly point into that repo, and
Codex does not substitute `${CLAUDE_PLUGIN_ROOT}` the way Claude Code does.
The Claude Code config at `.mcp.json` uses `${CLAUDE_PLUGIN_ROOT}` so it
remains portable across installs.

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
and runs shell commands on it directly over SSH. **Lambda Labs is the default
backend** (Modal is also supported). The agent calls `sandbox.options` /
`sandbox.request` / `sandbox.get` / `sandbox.terminal` / `sandbox.release`; it
never talks to the cloud provider directly.

Because Lambda sells fixed GPU+CPU+RAM machine types, the agent **selects the
hardware**: a `sandbox.request` with no `instance_type` (and no live sandbox)
returns `status: needs_selection` with a live, cheapest-first menu of currently
available machines, and the agent re-calls with a chosen `instance_type`.
`sandbox.options` lists availability without provisioning. On Modal the agent
instead passes `gpu`/`cpu`/`memory` directly and no selection step occurs.

Provisioning is **best-effort-synchronous**: creating a sandbox (large first
sync, cold GPU) can outlast the MCP call timeout, so `sandbox.request`
provisions on a background thread and waits up to a budget (default 45s,
`RESEARCH_PLUGIN_SANDBOX_REQUEST_WAIT`). If it comes up in time you get
`status: running` with `ssh.command` inline; otherwise you get
`status: provisioning` and **poll `sandbox.get`** (read-only) until it is
`running` or `failed`. `get` reconciles a provisioning row whose job died
(daemon restart) to `failed`, so a poll loop always terminates; the sandbox id
is persisted the instant the sandbox is created and a partial failure terminates
it, so a timed-out or canceled request never orphans a cloud sandbox.

Credentials go to the **HTTP daemon process** (the MCP proxy does not need
them), most simply via a git-ignored `.env` at the plugin root —
`research_plugin/.env` — which the daemon auto-detects. For the default Lambda
backend you only need an API key; region and instance type are chosen per
request (optionally defaulted via `RESEARCH_PLUGIN_LAMBDA_REGION` /
`RESEARCH_PLUGIN_LAMBDA_INSTANCE_TYPE`). For Modal
(`RESEARCH_PLUGIN_EXECUTION_BACKEND=modal`) set the Modal tokens:

```bash
# research_plugin/.env  (git-ignored)
LAMBDA_LABS_API_KEY=...        # default backend (Lambda Labs)

# Modal (only when RESEARCH_PLUGIN_EXECUTION_BACKEND=modal)
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
ed25519 keypair, creates a Modal sandbox or Lambda VM with `openssh-server`
running, exposes SSH, authorizes the public key, and returns SSH details. A
background **reaper** terminates any sandbox past its `time_limit`/`expires_at`
(after a final sync), so a Lambda VM — which has no server-side lifetime — never
bills past its deadline.
There is exactly one synced location per experiment: the experiment folder
`experiments/<name>/` (created automatically by `experiment.create`),
mirrored on the VM as `/workspace/<name>` (`$RP_EXPERIMENT_DIR`).
Fresh sandbox setup pushes the whole local folder there before returning
`status: running`. Everything outside that folder stays on the VM and dies
with it; large datasets and caches should be downloaded to `/workspace/data`
(exposed inside SSH commands as `$RP_DATASET_DIR` and `$RP_SANDBOX_DATA_DIR`),
the conventional remote-only scratch home.
If `HF_TOKEN` is present in the backend `.env` or process environment, sandbox
creation passes it with `modal.Secret.from_local_environ(["HF_TOKEN"])`, while
non-secret bootstrap values use `Sandbox.create(env=...)`. The SSH wrapper then
exports both `HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN` for Hugging Face tooling.
The token is never returned to agents; sandbox responses only advertise that the
env var is available.

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
its output to `/workspace/.research_plugin_sessions/<experiment>/transcript.log`
(outside the experiment folder — it is sandbox-authored telemetry, not
experiment content). `sandbox.terminal` reads it live from the sandbox; the UI
renders it as a per-experiment terminal window.

Durability: the wrapper runs every recorded command inside a detached tmux
session (both backends), so a command's lifetime is anchored to the VM rather
than to the SSH channel — dropped connections and timed-out agent calls stop
the viewing, not the work. Output keeps streaming into the transcript and the
`(exit N)` marker is written when the command ends, even with nobody connected.
Per-command run records (command, output, exit code) live under
`$RP_SANDBOX_DATA_DIR/.rp_runs/`. If tmux is unavailable the wrapper falls back
to the legacy attached execution.

For training runs the sandbox also boots an **MLflow tracking server** (port
5000) and a **TensorBoard** (port 6006) backed by the synced sessions directory.
Modal exposes them as HTTPS URLs through encrypted tunnels; Lambda Labs exposes
them through daemon-owned SSH local forwards. The sandbox row carries them as
`dashboards: {mlflow, tensorboard}` and the UI renders one iframe tab per
non-empty entry. `MLFLOW_TRACKING_URI=http://localhost:5000` is exported into
every SSH command, so Hugging Face `Trainer` and PyTorch Lightning's
`MLFlowLogger` can log to MLflow directly. Agents log run params, metrics, and
artifacts to MLflow and write TensorBoard events to `$RP_TB_LOGDIR`; for plain
PyTorch they can call `mlflow.autolog()` when useful. Stores live under
`/workspace/.research_plugin_sessions/<experiment>/{mlflow.db, mlflow-artifacts,
tb}`, outside the experiment folder; the daemon pulls them into
`.research_plugin/sessions/<experiment>/<sandbox_id>/` locally, one subdir per
VM generation, and archives MLflow metrics via REST so results outlive the VM.

`sandbox.sync` is the explicit live-sandbox visibility boundary: it mirrors
`/workspace/<name>` back to `experiments/<name>/` with SSH
rsync (`--delete`: an exact replica — deletions and renames propagate, local
edits are overwritten, so while a sandbox lives the VM owns the folder). A
best-effort background rsync also runs every few seconds while sandboxes are
active, and `sandbox.release` attempts one final pull before terminating the
sandbox/VM. Regular rsync excludes common heavy file types and applies a
conservative size cap. Deliberate large final artifacts belong under
`$RP_EXPERIMENT_DIR/artifacts_to_keep`, which syncs via a separate higher-size
rsync pass. Keep datasets, caches, checkpoints, parquet files, and scratch data
outside the experiment folder (e.g. `/workspace/data`).

Implemented MCP tools:

- `workflow.status_and_next`
- `project.current`, `project.create`, `project.update`, `project.get`
- `claim.create`, `claim.list`
- `experiment.create`, `experiment.list`, `experiment.get_state`, `experiment.transition`
- `resource.register_file` (single `path` or a `paths` batch), `resource.associate`, `resource.delete`, `resource.list`, `resource.resolve` (with `include_history` for observed versions)
- `review.request`, `review.start`, `review.submit`, `review.status`
- `sandbox.request`, `sandbox.get`, `sandbox.sync`, `sandbox.list`, `sandbox.release`, `sandbox.terminal`, `sandbox.health`
