# Hosted client quickstart

Set up a machine that runs agents against the hosted brain while keeping repo
access and caller SSH keys local.

## Install

```bash
git clone https://github.com/NGXT-Inc/Merv.git ~/Merv
```

Agent clients connect directly to the hosted brain over HTTP, so there is no
local proxy process to run. Cloning the repo provides the `merv-client`
onboarding CLI used below. `merv-client`, `merv-http`, the brain, and backend
tests run on Python 3.11+; a project environment is needed only for those
surfaces when 3.11+ is not already available. Sandbox SSH and explicit output
pulls use the system OpenSSH client and `rsync`.

## Authenticate with a project key

Each agent client authenticates to the hosted brain with a **project-scoped
key**. A key binds one immutable project: the gateway injects that project's id
into every project-scoped call, so agents never send a checkout path and the
brain never receives one. Mint a key in the UI:

1. Open [RapidReview](https://rapidreview.io/map) and sign in.
2. Open the project you want this client bound to.
3. Create a key for that project and copy it when shown.

A key is bearer-equivalent to full access to its one bound project, so treat it
like a password. Export it as `MERV_MCP_KEY` rather than storing it in a shared
config, and keep it out of shell history:

```bash
printf 'Paste the project key: '
IFS= read -r -s MERV_MCP_KEY
printf '\n'
export MERV_MCP_KEY
```

Add the `export` to your shell profile (or a `.env` you keep out of git) so
agent sessions inherit it. Never inline the key into a committed config file,
and keep any file that holds it listed in `.gitignore`.

Restart the agent session after changing the key so the MCP connection reloads
it.

## Connect a client

Every agent client — local Claude Code, cloud Codex, Replit, browser-driven —
connects the same way: directly to the brain's `POST /mcp` endpoint with the key
sent as `Authorization: Bearer ${MERV_MCP_KEY}`. Register the plugin in the
client using [CLIENTS.md](CLIENTS.md), then print the ready-to-paste http
snippet for this machine:

```bash
~/Merv/merv/bin/merv-client env
```

It emits the committed-config shape used by `.mcp.json` (and its
`.mcp.codex.json` / `mcp.json` siblings):

```json
{
  "mcpServers": {
    "merv": {
      "type": "http",
      "url": "https://experiments.rapidreview.io/mcp",
      "headers": { "Authorization": "Bearer ${MERV_MCP_KEY}" }
    }
  }
}
```

The key stays in the `MERV_MCP_KEY` env var and is never written into the file,
so the config is safe to commit while the key is not. Start an agent session
from any checkout: the gateway resolves the bound project from the key, so the
same config works from every folder and the checkout path never leaves the
machine.

The snippet points at the hosted brain by default. To target another brain — a
localhost dev brain at `http://127.0.0.1:8787/mcp`, or a self-hosted control
plane — set it once in the machine config so `merv-client env` emits the
matching `url` (or edit the `url` in the snippet directly):

```bash
~/Merv/merv/bin/merv-client configure \
  --control-url https://your-control-plane.example.com
```

## The `merv-client` CLI

The onboarding CLI has exactly two subcommands:

```bash
CLI=~/Merv/merv/bin/merv-client
$CLI configure   # write machine config (e.g. which brain to target)
$CLI env         # print the .mcp.json http snippet for this machine
```

The older `login`, `link`, `links`, `route`, and `unlink` subcommands are gone:
a project-scoped key now carries both authentication and the project binding, so
there is nothing to log in to, link, or unlink.
