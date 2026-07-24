# Client Support

The plugin targets seven agentic clients from one canonical content tree.
Everything heavy — state, gates, capability-based reviews, sandbox
provisioning — lives in the client-neutral brain service (localhost
`merv-http`, or the hosted brain). Every client — local Claude Code, cloud
Codex, Cursor, Gemini CLI, OpenCode, OpenHands, and Replit Agent — connects
directly to the brain's `POST /mcp` endpoint. Bundled clients authenticate with
a project-scoped key sent as `Authorization: Bearer <key>` from
`MERV_MCP_KEY`; browser-configured clients may instead use the hosted OAuth
flow or a pasted project key. A key binds one immutable project; the gateway
enforces that any project_id an agent passes equals the key-bound project (an
agent learns it once via `project current`, then passes it explicitly), so
agents never send a checkout root. Each client gets a thin adapter on top of the same `bin/`,
`skills/`, and `agents/` content:

| Client | Adapter | MCP registration | Skills | Reviewer subagents |
|---|---|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` + `.mcp.json` | http server → `<base>/mcp`, `Authorization: Bearer ${MERV_MCP_KEY}` | `skills/` auto-discovered | `agents/` auto-discovered (`merv:` namespace) |
| Codex | `.codex-plugin/plugin.json` + `.mcp.codex.json` | http server → `<base>/mcp` (same header) | `skills/` via manifest | spawned via review skills |
| Cursor | `.cursor-plugin/plugin.json` + `mcp.json` | http server → `<base>/mcp` (same header) | `skills/` auto-discovered (Agent Skills standard) | `agents/` auto-discovered |
| Gemini CLI | `gemini-extension.json` + `GEMINI.md` | http server → `<base>/mcp` (same header) | `skills/` auto-discovered (Agent Skills standard) | `agents/` auto-discovered |
| OpenCode | `clients/opencode/` (installer + agents + config example) | `opencode.json` `mcp` block → `<base>/mcp` (same header) | symlinked into `~/.config/opencode/skills/` | symlinked into `~/.config/opencode/agents/` |
| OpenHands | `AGENTS.md` + `clients/openhands/README.md` | local `config.toml` / CLI, or Cloud **Settings → MCP** | root `AGENTS.md`; optional repo copies in `.agents/skills/*.md` | none; second session/agent or inline |
| Replit Agent | `clients/replit/README.md` | account **MCP Servers** settings → `<base>/mcp` | no Merv skills installed by the connection | none; second session/agent or inline |

Shared invariants across all clients:

- The project is bound by the key, not by a checkout path: a `MERV_MCP_KEY`
  binds one immutable project, and the gateway enforces that the project_id an
  agent passes matches the key-bound project (learned once via `project current`,
  then passed explicitly on every project-scoped call). Agents never send a repo
  root, and no client needs to point Merv at a project directory.
- The MCP connection is plain HTTP, so a client needs no local Merv runtime to
  reach the brain — just an HTTP MCP server entry. The brain is the single
  source of truth for tool schemas (`contracts.py` TOOL_MANIFEST, served via
  `tools/list`); there is no checked-in client-side tool catalog. The
  `merv-client` onboarding CLI, `merv-http`, and brain remain Python 3.11+; a
  venv is needed only for those surfaces when the machine does not already
  provide 3.11+. Agent-run byte transfers — the presigned `curl` for
  `artifact.submit` / `storage.*` and the `rsync` for `sandbox.pull_outputs` —
  rely on the machine's `curl`, OpenSSH client, and `rsync`.
- Skills follow the cross-tool Agent Skills layout (`skills/<name>/SKILL.md`
  with `name` + `description` frontmatter), which Claude Code, Codex, Cursor,
  Gemini CLI, and OpenCode all read natively. OpenHands uses repository files
  at `.agents/skills/*.md`; copy the relevant canonical skill content into that
  layout when needed. Replit's account connection does not install Merv skills.
- Shared agent files in `agents/` keep frontmatter to the common subset
  (`name`, `description`) so Claude Code, Cursor, and Gemini CLI can all load
  them. OpenCode needs `mode`/`permission` frontmatter, so it has its own thin
  agent wrappers in `clients/opencode/agents/` that load the matching review
  skill.
- The committed manifests pin every bundled client to the hosted brain
  `https://experiments.rapidreview.io/mcp`, so out of the box each client dials
  the hosted brain and runs no local brain. Run `merv-client env` to print the
  ready-to-paste `.mcp.json` http snippet and `merv-client configure` to write
  machine config. For a local deployment, point the `url` at
  `http://127.0.0.1:8787/mcp` and start `bin/merv-http`. Export `MERV_MCP_KEY`
  in your shell before launching a client and keep it out of version control —
  the key is never inlined into a committed manifest, and it is
  bearer-equivalent to full access to its one bound project. OpenHands cannot
  ship the MCP connection in a repository, and Replit connections are
  account-scoped; configure those once through their documented setup surface.

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
- **OpenHands**: no reviewer-subagent auto-discovery; start a second session or
  agent with the matching review skill and handoff prompt, or follow it inline.
- **Replit Agent**: no reviewer-subagent auto-discovery; start a second session
  or agent with the matching review skill and handoff prompt, or follow it
  inline.

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

Two Cursor-specific notes:

1. **MCP server.** Cursor registers Merv as an HTTP MCP server. The bundled
   [mcp.json](../mcp.json) points at the hosted brain and carries the project
   key from the environment:

```json
{
  "mcpServers": {
    "merv": {
      "type": "http",
      "url": "https://experiments.rapidreview.io/mcp",
      "headers": {
        "Authorization": "Bearer ${MERV_MCP_KEY}"
      }
    }
  }
}
```

   Export `MERV_MCP_KEY` in your environment before launching Cursor; never
   inline the key into `mcp.json`. To point one workspace at a different brain
   (e.g. a local `http://127.0.0.1:8787/mcp` deployment), edit the `url` in the
   project's `.cursor/mcp.json`.

2. **Tool ceiling.** Cursor's approximately 40-tool limit applies across all
   active MCP servers. Merv's 36-tool catalog nearly fills it. Merv hides
   UI/internal tools such as `project.list` and `review.status` from the
   agent-facing catalog; if tools disappear when several MCP servers are
   enabled, disable servers or tools that are not in use.

Cursor's MCP settings may show a naming warning for dotted tools such as
`experiment.get_state`; the client still calls those tools normally.

## Use with Gemini CLI

The plugin ships a Gemini CLI extension: [gemini-extension.json](../gemini-extension.json)
bundles the MCP server (an HTTP server pointed at the brain's `/mcp` endpoint,
carrying the project key from `MERV_MCP_KEY`) and loads
[GEMINI.md](../GEMINI.md) as always-on context. `skills/` and `agents/` are
auto-discovered from the extension directory.

Install from a local checkout (or link for development):

```bash
gemini extensions install /path/to/merv
# or, during development:
gemini extensions link /path/to/merv
```

Notes:

- Reviewer subagents can be given genuinely separate MCP sessions on Gemini:
  an agent's inline `mcpServers` frontmatter opens its own connection to the
  brain. The shared agent files do not use this (they stay client-common); the
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

- The `opencode.json` `mcp` block registers Merv as a remote HTTP MCP server
  (the brain's `/mcp` endpoint with `Authorization: Bearer ${MERV_MCP_KEY}`),
  so there is no local process to spawn.
- The reviewer agents run as subagents (`mode: subagent`) with `edit`/`bash`
  denied; they load the matching review skill via OpenCode's native skill
  tool and submit through `review.start` / `review.submit`. Subagents get
  their own child session ids — pass them as `caller_session_id` for
  `verified_agent_review` status.
- OpenCode also reads `.claude/skills/` and `CLAUDE.md` as compatibility
  fallbacks, so repos already set up for Claude Code degrade gracefully.

## Use with OpenHands

OpenHands uses Streamable HTTP. For a local installation, put the server in
`config.toml`:

```toml
[mcp]
shttp_servers = [
  { url = "https://experiments.rapidreview.io/mcp", api_key = "paste the key" }
]
```

The `api_key` value is sent exactly as `Authorization: Bearer <value>`.
Environment-variable interpolation in that TOML value is unconfirmed, so paste
the project key minted in the RapidReview UI. `openhands mcp add` is the CLI
alternative. For OAuth, replace `api_key` with `auth = "oauth"`; the interactive
browser flow is unsuitable headless, so prefer the project key.

On OpenHands Cloud, **Settings → MCP** is the only setup path. The MCP
connection cannot be shipped in a repository. Repository-root
[AGENTS.md](../AGENTS.md) supplies always-on Merv context, and research repos
may copy canonical skill content into keyword-triggered
`.agents/skills/*.md`. Full steps:
[clients/openhands/README.md](../clients/openhands/README.md).

## Use with Replit Agent

Replit's custom remote MCP support is configured under **MCP Servers**:
select **+ Add MCP server**, enter a display name and
`https://experiments.rapidreview.io/mcp`, then select **Test & save**. Merv
advertises OAuth 2.1 dynamic client registration with PKCE through RFC 8414
discovery and the RFC 9728 protected-resource challenge, so Replit registers it
and guides the browser sign-in and consent flow.

Advanced settings accept custom header name/value pairs for static keys.
Replit's documentation demonstrates `X-API-Key`; accepting a literal
`Authorization: Bearer ...` pair is **unconfirmed**, so OAuth is the primary
path. Connections are account-scoped across repls and cannot be pre-wired by a
template or `.replit`. All MCP traffic passes Replit's security scanner; no
per-tool grants or tool-count ceiling are documented. Full steps:
[clients/replit/README.md](../clients/replit/README.md).
