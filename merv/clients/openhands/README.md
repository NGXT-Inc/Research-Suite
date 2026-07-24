# OpenHands

OpenHands connects directly to the Merv Streamable HTTP endpoint. The MCP
connection is machine- or account-level configuration and cannot be shipped in
a research repository; add it once, then let the repository supply agent
instructions.

## Mint a project key

Open [RapidReview](https://rapidreview.io/map), sign in, open or create the
project, and mint a key in the UI. Copy it when shown. The key is the bearer
credential for exactly one immutable project; treat it like a password.

See the [hosted client quickstart](../../docs/HOSTED_CLIENT_QUICKSTART.md) for
the full key flow.

## Local OpenHands

Add Merv to the OpenHands `config.toml`:

```toml
[mcp]
shttp_servers = [
  { url = "https://experiments.rapidreview.io/mcp", api_key = "paste the key" }
]
```

OpenHands sends an `api_key` value exactly as `Authorization: Bearer <value>`.
Environment-variable interpolation in this TOML value is unconfirmed, so paste
the key value rather than writing `${MERV_MCP_KEY}` there.

As a CLI alternative, run `openhands mcp add` and enter the same server URL and
project key.

For an attended OAuth setup, the local TOML variant is:

```toml
[mcp]
shttp_servers = [
  { url = "https://experiments.rapidreview.io/mcp", auth = "oauth" }
]
```

That launches an interactive browser flow. It is unsuitable for headless work;
prefer the `api_key` form for reliable agent sessions.

## OpenHands Cloud

The Settings UI is the only way to add an MCP server on OpenHands Cloud:

1. Open **Settings → MCP**.
2. Add `https://experiments.rapidreview.io/mcp`.
3. Paste the project key when prompted and save.

## Repository instructions

OpenHands always reads a repository-root `AGENTS.md`; it also recognizes the
same content through `CLAUDE.md` and `GEMINI.md` compatibility variants. This
adapter ships [AGENTS.md](../../AGENTS.md) as the platform-neutral Merv context.

OpenHands keyword-triggers repository skills from `.agents/skills/*.md` files
with `name` and `description` frontmatter. Research repositories may copy the
relevant content from [Merv's canonical skills](../../skills/) into that
layout. OpenHands does not auto-discover Merv reviewer subagents; hand a fresh
`review.request` prompt to a second session or agent following the matching
review skill, or perform the handoff inline when separate execution is
unavailable.
