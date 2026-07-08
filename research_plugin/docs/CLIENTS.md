# Client Support

The plugin targets five agentic clients from one canonical content tree.
Everything heavy — state, gates, capability-based reviews, sandbox
provisioning — lives in the client-neutral brain service (localhost
`research-plugin-http`, or the hosted brain). The stdio MCP proxy is
stdlib-only and always does the checkout-local data-plane work: repo reads,
hashing, validation, output pulls, caller SSH key custody, and folder-to-project
links. Each client gets a thin adapter on top of the same `bin/`, `skills/`,
and `agents/` content:

| Client | Adapter | MCP registration | Skills | Reviewer subagents |
|---|---|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` + `.mcp.json` | `${CLAUDE_PLUGIN_ROOT}` launcher path; cwd = project root | `skills/` auto-discovered | `agents/` auto-discovered (`research-plugin:` namespace) |
| Codex | `.codex-plugin/plugin.json` + `.mcp.codex.json` | plugin-relative launcher path; cwd = project root | `skills/` via manifest | spawned via review skills |
| Cursor | `.cursor-plugin/plugin.json` + `mcp.json` | `${workspaceFolder}` env var (cwd is NOT the workspace) | `skills/` auto-discovered (Agent Skills standard) | `agents/` auto-discovered |
| Gemini CLI | `gemini-extension.json` + `GEMINI.md` | `${extensionPath}` launcher, `${workspacePath}` env var | `skills/` auto-discovered (Agent Skills standard) | `agents/` auto-discovered |
| OpenCode | `clients/opencode/` (installer + agents + config example) | `opencode.json` `mcp` block; cwd = project root by default | symlinked into `~/.config/opencode/skills/` | symlinked into `~/.config/opencode/agents/` |

Shared invariants across all clients:

- The launcher [bin/research-plugin-mcp](../bin/research-plugin-mcp) resolves
  the project from `RESEARCH_PLUGIN_REPO_ROOT`, defaulting to `$PWD`. Clients
  that do not spawn stdio servers in the project directory (Cursor) must set
  the env var explicitly; the others rely on cwd.
- Skills follow the cross-tool Agent Skills layout (`skills/<name>/SKILL.md`
  with `name` + `description` frontmatter), which Claude Code, Codex, Cursor,
  Gemini CLI, and OpenCode all read natively.
- Shared agent files in `agents/` keep frontmatter to the common subset
  (`name`, `description`) so Claude Code, Cursor, and Gemini CLI can all load
  them. OpenCode needs `mode`/`permission` frontmatter, so it has its own thin
  agent wrappers in `clients/opencode/agents/` that load the matching review
  skill.
- The MCP proxy always dials `RESEARCH_PLUGIN_CONTROL_URL`. Local deployments
  use the default `http://127.0.0.1:8787` and require
  `bin/research-plugin-http` to be running. Hosted deployments set the same env
  var to the hosted brain URL and run no local brain.

## Long runs (rp_run) per client

Long sandbox work is client-neutral in core: launch with
`rp_run <label> -- <command>` over SSH, then check `sandbox.runs` — either a
`wait_seconds` long-poll inside the session or a plain call when next attending
the experiment. The long-poll cap is 300s server-side, but most MCP clients cut
tool calls around ~60s; unless you know your client's tool timeout is higher,
pass `wait_seconds<=45` (the same bound `sandbox.request` uses) and call again.

Optional per-client babysitting recipes — documentation only, nothing in core
depends on them:

- **Claude Code**: instead of blocking on a long-poll, start a background
  shell task that watches the sentinel and let the client's native
  background-task notification fire when it exits:
  `ssh <host> 'until [ -f $RP_EXPERIMENT_DIR/.runs/<label>/exit_code ]; do sleep 60; done'`
  (run in the background). The turn ends immediately; the notification brings
  the agent back, and one `sandbox.runs` call fetches the receipts.
- **Other clients** (Codex, Cursor, Gemini CLI, OpenCode): no background-task
  notification channel — end the turn after launching via rp_run and call
  `sandbox.runs` when next attending the experiment. Every sandbox.* response
  carries a one-line `runs` summary while runs exist, so a routine
  `sandbox.get` also surfaces finished work.

## Reviewer handoff per client

The backend's `workflow.status_and_next` returns a client-neutral
`launch_design_reviewer` / `launch_experiment_reviewer` /
`launch_reflection_reviewer` action plus a `reviewer_capability`. What differs
per client is only how the separate read-only reviewer agent is spawned:

- **Claude Code**: Agent tool with `subagent_type` set to
  `research-plugin:experiment-design-review` / `research-plugin:experiment-attempt-review` /
  `research-plugin:project-reflection-review`.
- **Codex**: spawn a reviewer agent with the matching review skill.
- **Cursor**: delegate to the plugin subagent (`/experiment-design-review`, or natural
  language); subagents run with a clean context window.
- **Gemini CLI**: the extension's agents are exposed as tools; the main agent
  delegates automatically, or the user forces it with `@experiment-design-review`.
- **OpenCode**: the main agent delegates via the task tool to the installed
  subagent (or the user @-mentions it, e.g. `@experiment-design-review`).

Independence is enforced server-side and identically everywhere: a one-time
capability pinned to a target snapshot, a read-only reviewer funnel, and a
producer-session check. `producer_session_id` / `caller_session_id` are
self-reported; when a client cannot supply a distinct caller session id the
review is recorded as `attested_agent_review` instead of
`verified_agent_review` (see [REVIEW_IDENTITY.md](REVIEW_IDENTITY.md)).

## Use with Cursor

The plugin ships a Cursor plugin bundle: [.cursor-plugin/plugin.json](../.cursor-plugin/plugin.json)
plus [mcp.json](../mcp.json) at plugin root; `skills/` and `agents/` are
auto-discovered from the same locations all other clients use.

For local development, link the plugin directory into
`~/.cursor/plugins/local/research-plugin` (symlinks are supported), then
enable it in Cursor.

Two Cursor-specific notes:

1. **Project root.** Cursor does not spawn stdio MCP servers in the workspace
   directory, so [mcp.json](../mcp.json) passes
   `"RESEARCH_PLUGIN_REPO_ROOT": "${workspaceFolder}"` — never rely on cwd.
2. **Launcher path.** The bundled `mcp.json` uses a plugin-relative command
   (`bin/research-plugin-mcp`). If your Cursor build does not resolve relative
   commands against the plugin root, register the server manually in the
   project's `.cursor/mcp.json` with the absolute path:

```json
{
  "mcpServers": {
    "research-plugin": {
      "type": "stdio",
      "command": "/absolute/path/to/research_plugin/bin/research-plugin-mcp",
      "env": {
        "RESEARCH_PLUGIN_REPO_ROOT": "${workspaceFolder}",
        "RESEARCH_PLUGIN_CONTROL_URL": "http://127.0.0.1:8787"
      }
    }
  }
}
```

(`RESEARCH_PLUGIN_CONTROL_URL` points at the brain. Use the localhost default
for local deployments, or a hosted HTTPS URL for hosted deployments.)

The plugin exposes ~55 MCP tools; Cursor historically limits the number of
active tools (~40 community-reported), so the plugin alone can exceed that
ceiling. Disable unused MCP servers in the workspace and allowlist the
research tools you need.

## Use with Gemini CLI

The plugin ships a Gemini CLI extension: [gemini-extension.json](../gemini-extension.json)
bundles the MCP server (launcher via `${extensionPath}`, project root via
`${workspacePath}`) and loads [GEMINI.md](../GEMINI.md) as always-on context.
`skills/` and `agents/` are auto-discovered from the extension directory.

Install from a local checkout (or link for development):

```bash
gemini extensions install /path/to/research_plugin
# or, during development:
gemini extensions link /path/to/research_plugin
```

Notes:

- Reviewer subagents can be given genuinely separate MCP sessions on Gemini:
  an agent's inline `mcpServers` frontmatter spawns its own proxy process. The
  shared agent files do not use this (they stay client-common); the
  capability + producer-session checks are the load-bearing independence
  mechanism regardless.
- Google announced (May 2026) a transition from Gemini CLI to Antigravity
  CLI; extensions, skills, subagents, and hooks reportedly carry over as
  Antigravity plugins. API-key and Code Assist users are unaffected by the
  June 2026 sign-in cutoff. Treat this adapter as best-effort until the
  Antigravity plugin surface is published.

## Use with OpenCode

OpenCode has no declarative plugin bundle, so the adapter is an installer:

```bash
/path/to/research_plugin/clients/opencode/install.sh
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
