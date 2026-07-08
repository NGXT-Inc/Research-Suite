# Hosted client quickstart

Setup for a machine that runs agents against the hosted brain.

## Install

```bash
git clone <research-suite-repo-url> ~/research-suite
```

Nothing else: the proxy runs on bare `python3` (3.11+), no pip installs and no
token. (A venv with `pip install -e .` is only needed to run a local brain.)

## Set up

1. Register the plugin in your client — per-client steps in
   [CLIENTS.md](CLIENTS.md).
2. Open a research repo and start a session. The proxy dials the hosted brain
   by default; when the project tool (`action: "current"`) reports the folder is
   unlinked, the agent asks which project to use and calls the project tool with
   `action: "connect"` — an existing `project_id`, or a `name`/`summary` to
   create one and link it in one step.

Repeat step 2 in each additional checkout. Links and machine config live under
`~/.research_plugin/`, never inside a research repo, and the brain never sees
folder paths — only project ids.

To point the machine at a different brain:

```bash
~/research-suite/research_plugin/bin/research-plugin-client configure \
  --control-url https://your-control-plane.example.com
```

## CLI fallback

Everything the agent does with the project tool (`action: "connect"`) can be
done from a terminal (run from the checkout, or pass `--repo`):

```bash
CLI=~/research-suite/research_plugin/bin/research-plugin-client
$CLI link --project-id proj_123   # link this folder
$CLI links                        # list all links on this machine
$CLI route                        # show this folder's project
$CLI unlink                       # remove this folder's link
$CLI mcp-env                      # print env vars for a manual MCP config
```
