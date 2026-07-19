# Merv

Merv gives agentic coding clients (Claude Code, Codex, Cursor,
Gemini CLI, OpenCode) a shared state machine for machine learning research:
claims, experiments, repo-file resources, review gates, reflection waves, and
sandboxed execution. A brain running locally or as a hosted service owns durable
research state; a small stdio MCP proxy runs on your machine and does the
checkout-local file work. The brain never receives the checkout root or reads
it directly, although the proxy explicitly submits relative resource metadata
and size-capped evidence bytes.

## Get started

```bash
git clone https://github.com/NGXT-Inc/Merv.git ~/Merv
```

That is the whole Python install — the proxy runs on bare `python3` (3.11+),
with no pip packages. The launcher uses a POSIX shell; sandbox SSH and explicit
output pulls also use the system OpenSSH client and `rsync`. Then:

1. Register the plugin in your client — per-client steps in
   [docs/CLIENTS.md](docs/CLIENTS.md).
2. For the hosted brain, authenticate the machine once with a RapidReview API
   key — see [docs/HOSTED_CLIENT_QUICKSTART.md](docs/HOSTED_CLIENT_QUICKSTART.md).
3. Open your research repo and start a session:

```text
Use Merv. Start with project(action="current"), then workflow.status_and_next.
```

The proxy dials the hosted brain by default. On first use the agent asks which
project this folder belongs to and links it with `project(action="connect")` —
no terminal setup. Details and the CLI fallback:
[docs/HOSTED_CLIENT_QUICKSTART.md](docs/HOSTED_CLIENT_QUICKSTART.md).

## How work moves

Experiments move forward through two review gates; a rejected review sends the
work back (dashed):

```mermaid
flowchart LR
    planned --> design_review --> ready_to_run --> running --> experiment_review --> complete
    design_review -. revise plan .-> planned
    experiment_review -. plan flawed .-> planned
    experiment_review -. keep working .-> running
```

Reflections distill what the project has learned, behind one gate of their own:

```mermaid
flowchart LR
    reflecting --> synthesizing --> reflection_review --> published
    reflection_review -. revise lenses .-> reflecting
    reflection_review -. revise synthesis .-> synthesizing
```

Agent-authored evidence is kept in regular repo files. The brain records their
relative paths and versions and pins selected submitted bytes for gates and
review. System metrics exhibits and optional heavy storage objects are separate
brain-managed artifacts.

## Running a local brain (optional)

For development, or to keep all state on your machine:

```bash
cd /path/to/merv
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./bin/merv-http --host 127.0.0.1 --port 8787
bin/merv-client configure --control-url http://127.0.0.1:8787
```

Sandbox provider credentials (Lambda Labs by default; Thunder, Modal, and a
fake test backend via `MERV_EXECUTION_BACKEND`) belong to the brain
process only — see `.env.example`. Startup details:
[docs/STARTUP_CHEATSHEET.md](docs/STARTUP_CHEATSHEET.md).

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

Set `MERV_EXECUTION_BACKEND=fake` to keep tests and local workflows
off cloud providers.

## Documentation

- [docs/CLIENTS.md](docs/CLIENTS.md) - per-client install and reviewer handoff
- [docs/HOSTED_CLIENT_QUICKSTART.md](docs/HOSTED_CLIENT_QUICKSTART.md) - hosted setup
- [docs/AUTH.md](docs/AUTH.md) - hosted authentication and project membership
- [docs/STARTUP_CHEATSHEET.md](docs/STARTUP_CHEATSHEET.md) - local startup flow
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - backend and mode architecture
- [docs/CONTROL_DATA_PLANE_SPLIT.md](docs/CONTROL_DATA_PLANE_SPLIT.md) - brain/proxy ownership boundary
- [docs/MODULE_BOUNDARIES.md](docs/MODULE_BOUNDARIES.md) - enforced backend dependency law
- [docs/MCP_SERVER_CONTRACT.md](docs/MCP_SERVER_CONTRACT.md) - MCP tools and contracts
- [docs/WORKFLOW_AND_REVIEW.md](docs/WORKFLOW_AND_REVIEW.md) - workflow gates and reviews
- [docs/REVIEW_IDENTITY.md](docs/REVIEW_IDENTITY.md) - reviewer session and capability boundary
- [docs/RESOURCE_MODEL.md](docs/RESOURCE_MODEL.md) - repo-file resource model
- [docs/STORAGE_MODEL.md](docs/STORAGE_MODEL.md) - optional heavy-object storage
- [docs/CENTRALIZED_MLFLOW.md](docs/CENTRALIZED_MLFLOW.md) - centralized MLflow tracking
- [docs/UI_API.md](docs/UI_API.md) - frontend HTTP API
- [docs/CONTROL_PLANE_OPERATIONS.md](docs/CONTROL_PLANE_OPERATIONS.md) - hosted operations and security boundary
- [deploy/README.md](deploy/README.md) - reference control-plane deploy
