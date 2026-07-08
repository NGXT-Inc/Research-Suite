# Hosted client quickstart

Use this when the control plane is already hosted and this machine/VM is only
running agents against local checkouts.

## Shape

One hosted control plane serves project records and gates. Each client machine
uses the stdio MCP proxy as its local data plane, and each local checkout is
linked to the hosted project it should work on. The machine config does not
contain one repo path or one project id; folder links live in a machine-local
SQLite link file under `~/.research_plugin/`.

## Install on the client VM

```bash
git clone <research-suite-repo-url> ~/research-suite
cd ~/research-suite/research_plugin

python3 -m venv .venv
.venv/bin/pip install -e .
```

## Fast path: register the MCP and let the agent link

With the plugin registered in your client (see [CLIENTS.md](CLIENTS.md)), no
terminal setup is required: the proxy dials the hosted brain
(`https://experiments.rapidreview.io`) by default, and the agent links each
checkout from inside the session. When `project.current` reports
`exists: false`, the agent asks which project to use and calls
`project.connect` — with a `project_id` to link an existing hosted project,
or with a user-confirmed `name`/`summary` to create one and link it in one
step. The folder→project link is written machine-locally; the brain never
sees the folder path.

For a different control plane, configure it once per machine:

```bash
~/research-suite/research_plugin/bin/research-plugin-client configure \
  --control-url https://your-control-plane.example.com
```

The current operator-run setup uses a private control plane, so the client does
not need a control-plane token.

## CLI fallback: link from the terminal

The same links can be managed without an agent session:

```bash
cd ~/work/project-a
~/research-suite/research_plugin/bin/research-plugin-client link --project-id proj_123
```

## What gets saved

Machine-local config and folder links are written under `~/.research_plugin/`;
they are not part of any research repo.

## Link more local folders

Each additional checkout links the same way: the agent calls
`project.connect` from a session opened in that folder, or from the terminal:

```bash
cd ~/work/project-b
~/research-suite/research_plugin/bin/research-plugin-client link --project-id proj_456
```

Inspect links:

```bash
~/research-suite/research_plugin/bin/research-plugin-client links
~/research-suite/research_plugin/bin/research-plugin-client route --repo ~/work/project-a
```

Remove a link:

```bash
~/research-suite/research_plugin/bin/research-plugin-client unlink --repo ~/work/project-a
```

## Agent/MCP environment

The packaged MCP proxy auto-discovers `~/.research_plugin/client.json`. For a
manual MCP config, print the exact values:

```bash
~/research-suite/research_plugin/bin/research-plugin-client mcp-env --repo "$PWD"
```

The repo folder is temporary local context. The hosted project remains the
source of truth for project records, gates, reviews, and sandbox lifecycle.
