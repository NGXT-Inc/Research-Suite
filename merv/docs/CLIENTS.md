# Client Support

The plugin targets five agentic clients from one canonical content tree.
Everything heavy — state, gates, capability-based reviews, sandbox
provisioning — lives in the client-neutral brain service (localhost
`merv-http`, or the hosted brain). The stdio MCP proxy is
stdlib-only and always does the checkout-local data-plane work: repo reads,
hashing, validation, output pulls using caller-provided SSH key paths, and
folder-to-project links. It does not mint or persist caller private keys. Each
client gets a thin adapter on top of the same `bin/`, `skills/`, and `agents/`
content:

| Client | Adapter | MCP registration | Skills | Reviewer subagents |
|---|---|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` + `.mcp.json` | `${CLAUDE_PLUGIN_ROOT}` launcher path; cwd = project root | `skills/` auto-discovered | `agents/` auto-discovered (`merv:` namespace) |
| Codex | `.codex-plugin/plugin.json` + `.mcp.codex.json` | plugin-relative launcher path; cwd = project root | `skills/` via manifest | spawned via review skills |
| Cursor | `.cursor-plugin/plugin.json` + `mcp.json` | `${workspaceFolder}` env var (cwd is NOT the workspace) | `skills/` auto-discovered (Agent Skills standard) | `agents/` auto-discovered |
| Gemini CLI | `gemini-extension.json` + `GEMINI.md` | `${extensionPath}` launcher, `${workspacePath}` env var | `skills/` auto-discovered (Agent Skills standard) | `agents/` auto-discovered |
| OpenCode | `clients/opencode/` (installer + agents + config example) | `opencode.json` `mcp` block; cwd = project root by default | symlinked into `~/.config/opencode/skills/` | symlinked into `~/.config/opencode/agents/` |

Shared invariants across all clients:

- The launcher [bin/merv-mcp](../bin/merv-mcp) resolves
  the project from `MERV_REPO_ROOT`, defaulting to `$PWD`. Clients
  that do not spawn stdio servers in the project directory (Cursor) must set
  the env var explicitly; the others rely on cwd.
- The proxy needs no pip installs: it runs on bare `python3` (3.11+). The
  tool catalog ships as checked-in JSON (`src/merv/proxy/_tool_catalog.json`) so
  discovery works without third-party packages; optional local prevalidation
  uses the live Pydantic contracts when they are already installed and otherwise
  preserves the bare-Python path. A venv is only needed to run a local brain.
  The launcher expects a POSIX shell, and sandbox SSH/output pulls rely on the
  machine's OpenSSH client and `rsync`.
- Skills follow the cross-tool Agent Skills layout (`skills/<name>/SKILL.md`
  with `name` + `description` frontmatter), which Claude Code, Codex, Cursor,
  Gemini CLI, and OpenCode all read natively.
- Shared agent files in `agents/` keep frontmatter to the common subset
  (`name`, `description`) so Claude Code, Cursor, and Gemini CLI can all load
  them. OpenCode needs `mode`/`permission` frontmatter, so it has its own thin
  agent wrappers in `clients/opencode/agents/` that load the matching review
  skill.
- The MCP proxy resolves its brain URL as: `MERV_CONTROL_URL` env
  var > machine config written by `merv-client configure` >
  the hosted brain `https://experiments.rapidreview.io`. Out of the box every
  client therefore dials the hosted brain and runs no local brain. For a local
  deployment, run `merv-client configure --control-url
  http://127.0.0.1:8787` (or set the env var) and start
  `bin/merv-http`. Shipped manifests leave the env var empty on
  purpose — pinning a URL there would shadow the machine config.

## Long runs (merv_run) per client

Long sandbox work is client-neutral in core: launch with
`merv_run <label> -- <command>` over SSH, then check `sandbox.runs` — either a
`wait_seconds` long-poll inside the session or a plain call when next attending
the experiment. The long-poll cap is 300s server-side, but most MCP clients cut
tool calls around ~60s; unless you know your client's tool timeout is higher,
pass `wait_seconds<=45` (the same bound `sandbox.request` uses) and call again.

Optional per-client babysitting recipes — documentation only, nothing in core
depends on them:

- **Claude Code**: instead of blocking on a long-poll, start a background
  shell task that watches the sentinel and let the client's native
  background-task notification fire when it exits:
  `ssh <host> 'until [ -f $MERV_EXPERIMENT_DIR/.runs/<label>/exit_code ]; do sleep 60; done'`
  (run in the background). The turn ends immediately; the notification brings
  the agent back, and one `sandbox.runs` call fetches the receipts.
- **Other clients** (Codex, Cursor, Gemini CLI, OpenCode): no background-task
  notification channel — end the turn after launching via merv_run and call
  `sandbox.runs` when next attending the experiment. Run-oriented sandbox
  responses include compact receipts; `sandbox.runs` is the authoritative
  status/readback call.

## Reviewer handoff per client

`workflow.status_and_next` reports the active review gate and tells the main
agent when to request or launch a reviewer. The capability does **not** come
from that status response. The main agent calls `review.request`; that response
returns the short-lived `reviewer_capability` and a
`reviewer_handoff.spawn_prompt` containing the matching skill, request id, and
capability. What differs per client is only how the separate read-only reviewer
agent is spawned with that prompt:

- **Claude Code**: Agent tool with `subagent_type` set to
  `merv:experiment-design-review` / `merv:experiment-attempt-review` /
  `merv:project-reflection-review`.
- **Codex**: spawn a reviewer agent with the matching review skill.
- **Cursor**: delegate to the plugin subagent (`/experiment-design-review`, or natural
  language); subagents run with a clean context window.
- **Gemini CLI**: the extension's agents are exposed as tools; the main agent
  delegates automatically, or the user forces it with `@experiment-design-review`.
- **OpenCode**: the main agent delegates via the task tool to the installed
  subagent (or the user @-mentions it, e.g. `@experiment-design-review`).

The reviewer begins with `review.start`, passing the request id, capability,
and its own non-empty `caller_session_id`. That id is required and must differ
from the `producer_session_id` recorded by `review.request`. The brain stores
only a hash of the capability, pins the request to the target snapshot, rejects
stale or superseded requests, and rechecks the snapshot at submission.

New sessions that pass the distinct-id check are recorded as
`verified_agent_review`; `attested_agent_review` remains only on legacy rows.
The session ids are supplied by the clients, so this is workflow-level
separation rather than cryptographic proof of separate execution. See
[REVIEW_IDENTITY.md](REVIEW_IDENTITY.md).

## Use with Cursor

The plugin ships a Cursor plugin bundle: [.cursor-plugin/plugin.json](../.cursor-plugin/plugin.json)
plus [mcp.json](../mcp.json) at plugin root; `skills/` and `agents/` are
auto-discovered from the same locations all other clients use.

For local development, **copy** the plugin directory into
`~/.cursor/plugins/local/merv`, then enable it in Cursor. Cursor rejects
symlinks whose target is outside `~/.cursor/plugins/local` (you will see
`loadUserLocalPlugin merv rejected: symlink target ... is outside ...` in
the Cursor Plugins log). Re-`rsync` after editing the checkout, or keep a
real directory under `plugins/local` as your working tree.

If the machine's default `python3` is older than 3.11, create a venv in the
local install so the launcher does not fall through to a broken interpreter:
`python3.11 -m venv ~/.cursor/plugins/local/merv/.venv`.

Three Cursor-specific notes:

1. **Project root.** Cursor does not spawn stdio MCP servers in the workspace
   directory, so [mcp.json](../mcp.json) passes
   `"MERV_REPO_ROOT": "${workspaceFolder}"` — never rely on cwd.
   (If a client ever passes the variable through unexpanded, the launcher now
   fails loudly instead of silently binding to the spawn cwd.)
2. **Launcher path.** Cursor requires stdio commands to be on PATH or a full
   path — there is no plugin-relative resolution and no `${pluginRoot}`
   variable. The bundled `mcp.json` therefore uses
   `${userHome}/.cursor/plugins/local/merv/bin/merv-mcp`,
   which is correct for the documented install location (Cursor interpolates
   variables in `command`). If you install the plugin anywhere else, register
   the server manually in the project's `.cursor/mcp.json` with the absolute
   path:

```json
{
  "mcpServers": {
    "merv": {
      "type": "stdio",
      "command": "/absolute/path/to/merv/bin/merv-mcp",
      "env": {
        "MERV_REPO_ROOT": "${workspaceFolder}",
        "MERV_CONTROL_URL": ""
      }
    }
  }
}
```

(Leave `MERV_CONTROL_URL` empty so the machine config from
`merv-client configure` wins, falling back to the hosted brain.
Set it explicitly only to force one workspace onto a different brain, e.g.
`http://127.0.0.1:8787` for a local deployment.)

3. **Tool ceiling.** Cursor limits the combined active tools from all MCP
   servers. Merv hides UI/internal tools such as `project.list` and
   `review.status` from the agent-facing catalog. If tools disappear when
   several MCP servers are enabled, disable servers or tools that are not in
   use.

Cursor's MCP settings may show a naming warning for dotted tools such as
`experiment.get_state`; the client still calls those tools normally.

## Use with Gemini CLI

The plugin ships a Gemini CLI extension: [gemini-extension.json](../gemini-extension.json)
bundles the MCP server (launcher via `${extensionPath}`, project root via
`${workspacePath}`) and loads [GEMINI.md](../GEMINI.md) as always-on context.
`skills/` and `agents/` are auto-discovered from the extension directory.

Install from a local checkout (or link for development):

```bash
gemini extensions install /path/to/merv
# or, during development:
gemini extensions link /path/to/merv
```

Notes:

- Reviewer subagents can be given genuinely separate MCP sessions on Gemini:
  an agent's inline `mcpServers` frontmatter spawns its own proxy process. The
  shared agent files do not use this (they stay client-common); the
  capability + producer-session checks are the load-bearing independence
  mechanism regardless.

## Use with OpenCode

OpenCode has no declarative plugin bundle, so the adapter is an installer:

```bash
/path/to/merv/clients/opencode/install.sh
```

It symlinks the canonical skills into `~/.config/opencode/skills/`, the
OpenCode reviewer agents into `~/.config/opencode/agents/`, and prints the
`opencode.json` `mcp` block to add (see
[clients/opencode/opencode.json.example](../clients/opencode/opencode.json.example)).

Notes:

- OpenCode spawns local MCP servers with cwd = project root, so the launcher
  needs no extra configuration.
- The reviewer agents run as subagents (`mode: subagent`) with `edit`/`bash`
  denied; they load the matching review skill via OpenCode's native skill
  tool and submit through `review.start` / `review.submit`. Subagents get
  their own child session ids — pass them as `caller_session_id` for
  `verified_agent_review` status.
- OpenCode also reads `.claude/skills/` and `CLAUDE.md` as compatibility
  fallbacks, so repos already set up for Claude Code degrade gracefully.
