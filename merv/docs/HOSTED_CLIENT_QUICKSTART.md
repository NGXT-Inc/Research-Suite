# Hosted client quickstart

Set up a machine that runs agents against the hosted brain while keeping repo
access and caller SSH keys local.

## Install

```bash
git clone <merv-repo-url> ~/Merv
```

The stdio proxy runs on bare `python3` 3.11+ and needs no pip install or access
token. Its launcher requires a POSIX shell; sandbox SSH and explicit output
pulls use the system OpenSSH client and `rsync`. A project environment is needed
only when running a localhost brain or the backend test suite.

## Connect a checkout

1. Register the plugin in the client using [CLIENTS.md](CLIENTS.md).
2. Open the research checkout and start an agent session.
3. Call the `project` tool with `action: "current"`.
4. If the folder is unlinked, choose one of these validated paths:
   - link an existing brain project with `action: "connect"` and its
     `project_id`; or
   - create and link a project in one step with `action: "connect"`, a
     user-confirmed `name`, and a short `summary`.

`project(action="connect")` checks an existing id with the brain before writing
the local link. A folder already linked to a different project requires
`overwrite: true` before it can be relinked.

Repeat this for each checkout. The folder-to-project database and machine
configuration live under `~/.research_plugin/` by default, never in the
research repo. The proxy sends only the selected project id to the brain; the
checkout path stays local.

An unconfigured proxy uses the hosted brain. To point this machine at another
brain, write the machine config:

```bash
~/Merv/merv/bin/merv-client configure \
  --control-url https://your-control-plane.example.com
```

Use `http://127.0.0.1:8787` only when a localhost brain is running there. An
explicit `RESEARCH_PLUGIN_CONTROL_URL` in an MCP configuration overrides the
machine setting; shipped manifests leave it empty so machine configuration can
take effect.

## Local link CLI

The CLI can inspect or directly edit the machine-local folder link database:

```bash
CLI=~/Merv/merv/bin/merv-client
$CLI link --project-id proj_123   # write/replace this folder's local mapping
$CLI links                        # list local mappings on this machine
$CLI route                        # show this folder's stored mapping
$CLI unlink                       # remove this folder's mapping
$CLI mcp-env                      # print env vars for a manual MCP config
```

Run those commands from the checkout or pass `--repo /path/to/checkout`.

`link` accepts a `project_id` rather than a name/summary, writes it locally, and
replaces any mapping for the same folder. It does **not** contact the brain,
validate that the id exists, or create a project. Prefer
`project(action="connect")` for normal setup; use the CLI when the id is already
known and a local-only mapping operation is intentional.
