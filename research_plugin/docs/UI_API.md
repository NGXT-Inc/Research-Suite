# UI API

The UI talks to a lightweight HTTP adapter over the same services used by MCP.
There is no agent chat endpoint in this backend. Chat remains in Codex.

Install HTTP/backend dependencies once:

```bash
cd /path/to/research_plugin
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

The HTTP launcher uses `.venv/bin/python` automatically when that virtualenv
exists. Set `RESEARCH_PLUGIN_PYTHON=/path/to/python` to force a different
interpreter.

Run the HTTP API:

```bash
research_plugin/bin/research-plugin-http --repo /path/to/research-repo --host 127.0.0.1 --port 8787
```

For auto-reload while editing backend code:

```bash
cd /Users/guraltoo/Documents/dev/proj/experiments/Papyrus/research_plugin
python3 scripts/dev_http_reload.py \
  --repo /path/to/research-repo \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

The HTTP launcher uses code from the installed plugin but stores state in the
target research repo. The default store path is:

```text
/path/to/research-repo/.research_plugin/state.sqlite
```

Activity is appended as JSONL beside the state DB:

```text
/path/to/research-repo/.research_plugin/activity.jsonl
```

The UI can read recent activity through `GET /api/activity?limit=100`. For live
terminal visibility across both HTTP and Codex-started MCP tool calls, use:

```bash
tail -f /path/to/research-repo/.research_plugin/activity.jsonl
```

## Principles

- UI reads project state from HTTP.
- Codex and reviewer agents use MCP.
- Both HTTP and MCP share the same SQLite state and service layer.
- UI may create claims, experiments, resources, transitions, and reviews, but it
  should not include an agent chat surface in this version.
- Resources are local repo files. Registering a resource stores a pointer and
  observed file metadata; it does not upload bytes.
- Project scope is explicit. The UI must select or create a project and use
  project-routed endpoints; the backend does not infer an active project.

## Health

```http
GET /health
```

Returns:

```json
{
  "ok": true,
  "version": "0.0004",
  "repo_root": "/path/to/repo",
  "store": "/path/to/.research_plugin/state.sqlite",
  "activity_log": "/path/to/.research_plugin/activity.jsonl"
}
```

## Activity

```http
GET /api/activity?limit=100
```

Returns recent backend activity events from `.research_plugin/activity.jsonl`.
Events include HTTP requests and MCP/HTTP tool calls. Tool-call arguments are
summarized to IDs and workflow fields. Successful tool-call events include the
full tool `result` returned to Codex or the HTTP adapter. This is intentionally
not truncated by the backend; the UI should use progressive disclosure or a
scrolling detail panel for large responses.

Reviewer capabilities can appear in `result` for `review.request`, because that
is part of the actual response sent to Codex. Treat the activity log as local
developer/debug data, not as a public audit feed.

```json
{
  "activity_log": "/path/to/repo/.research_plugin/activity.jsonl",
  "events": [
    {
      "event": "tool.call",
      "source": "mcp",
      "tool": "workflow.status_and_next",
      "status": "ok",
      "duration_ms": 2,
      "args": {
        "project_id": "proj_..."
      },
      "result": {
        "workflow": {
          "current_gate": "project_setup",
          "next_action": "create_claim_or_experiment"
        }
      },
      "ts": "2026-05-17T20:45:00Z"
    }
  ]
}
```

## Projects

```http
GET /api/projects
POST /api/projects
GET /api/projects/{project_id}
PATCH /api/projects/{project_id}
GET /api/projects/{project_id}/home
GET /api/projects/{project_id}/status?experiment_id={experiment_id}
```

Create payload:

```json
{
  "name": "Toy Length Classifier",
  "summary": "Evaluate a threshold classifier on a toy dataset."
}
```

`/home` is the main bootstrap endpoint. It returns:

- `project`
- `claims`
- `experiments`
- `resources`
- `reviews`
- `recent_events`
- `stats`
- `workflow`
- `active_experiment`
- `active_experiments`
- `active_processes`

`experiments` remains the full project experiment list. `active_experiments`
contains non-terminal experiments only (`planned`, `design_review`,
`ready_to_run`, `running`, `experiment_review`), sorted by active-work priority
and then recency. Each active experiment includes its current `workflow`, all
known `jobs`, and its active `active_processes`.

`active_processes` contains active execution job rows with status `submitting`,
`queued`, or `running`, plus `process_type: "execution_job"` and a compact
`experiment` summary.

## Claims

```http
GET /api/projects/{project_id}/claims
POST /api/projects/{project_id}/claims
GET /api/projects/{project_id}/claims/{claim_id}
```

Create payload:

```json
{
  "statement": "A length-threshold classifier improves accuracy.",
  "scope": "toy.csv only",
  "confidence": "medium"
}
```

## Experiments

```http
GET /api/projects/{project_id}/experiments
GET /api/projects/{project_id}/experiments?status=running
GET /api/projects/{project_id}/experiments/view
GET /api/projects/{project_id}/experiments/{experiment_id}
GET /api/projects/{project_id}/experiments/{experiment_id}/status
POST /api/projects/{project_id}/experiments
POST /api/projects/{project_id}/experiments/{experiment_id}/transition
```

Create payload:

```json
{
  "intent": "Compare threshold rule against majority baseline.",
  "claim_ids": ["claim_..."]
}
```

Transition payload:

```json
{
  "transition": "submit_design"
}
```

Supported transitions in v0.0001:

- `submit_design`
- `mark_ready_to_run`
- `start_running`
- `submit_results`
- `complete`
- `mark_failed`
- `abandon`

The UI should generally use `workflow.next_action` from status/home to decide
which action button to show.

Review gates stay as user-facing stages such as `design_review` and
`experiment_review`. When present, `workflow.review_gate` gives the substate:

- `none`: Needs reviewer.
- `requested`: Reviewer pending.
- `started`: Reviewer active.

The UI should render this as detail inside the review stage, not as a separate
top-level stage.

## Resources

```http
GET /api/projects/{project_id}/resources
GET /api/projects/{project_id}/resources?kind=result
GET /api/projects/{project_id}/resources/tree
GET /api/projects/{project_id}/resources/{resource_id}
GET /api/projects/{project_id}/resources/{resource_id}/history
GET /api/projects/{project_id}/resources/{resource_id}/content
GET /api/projects/{project_id}/resources/{resource_id}/file
POST /api/projects/{project_id}/resources
POST /api/projects/{project_id}/resources/{resource_id}/associate
```

Register payload:

```json
{
  "path": "experiments/e001/results.json",
  "kind": "result",
  "title": "Attempt 3 results"
}
```

Associate payload:

```json
{
  "target_type": "experiment",
  "target_id": "exp_...",
  "role": "result"
}
```

Experiment resource associations are attempt-scoped by the backend and include
the exact `version_id` associated to that attempt. `/content` and `/file` read
the current live file. Historical version content is not served by the backend —
`history` returns version metadata (sha256, size, mtime, content_type) only.

## Reviews

```http
GET /api/projects/{project_id}/reviews
GET /api/projects/{project_id}/reviews?target_type=experiment&target_id={experiment_id}
POST /api/projects/{project_id}/reviews/request
POST /api/projects/{project_id}/reviews/start
POST /api/projects/{project_id}/reviews/submit
```

The UI can show review history and open review requests. In normal use, reviewer
agents submit reviews through MCP, not the UI.

Request payload:

```json
{
  "target_type": "experiment",
  "target_id": "exp_...",
  "role": "design_reviewer",
  "reason": "Plan gate"
}
```

Roles:

- `design_reviewer`
- `experiment_reviewer`
- `human`
- `automated_check`

Verdicts:

- `pass`
- `needs_changes`
- `fail`

## Jobs

Execution jobs are exposed through HTTP for visibility and light control. The
UI should not talk to execution providers directly.

```http
GET /api/projects/{project_id}/jobs
GET /api/projects/{project_id}/jobs?experiment_id={experiment_id}
GET /api/projects/{project_id}/jobs?status=running
POST /api/projects/{project_id}/jobs
GET /api/projects/{project_id}/jobs/health
GET /api/projects/{project_id}/jobs/{job_id}
GET /api/projects/{project_id}/jobs/{job_id}/logs?tail=200
GET /api/projects/{project_id}/jobs/{job_id}/outputs
POST /api/projects/{project_id}/jobs/{job_id}/cancel
```

Submit payload:

```json
{
  "experiment_id": "exp_...",
  "command": "python scripts/train.py",
  "cwd": ".",
  "expected_outputs": [
    "experiments/e001/results.json"
  ]
}
```

MCP validates command, cwd, env, and output paths before delegating to the
configured execution backend. `backend_hints` is optional and opaque to
JobService.

## Events

```http
GET /api/projects/{project_id}/events?limit=100
```

Returns recent accepted state events. Useful for a compact timeline or debug
drawer.
