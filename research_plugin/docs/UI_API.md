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

Run the shared HTTP API:

```bash
research_plugin/bin/research-plugin-http --host 127.0.0.1 --port 8787
```

In shared mode, `POST /api/projects` supplies the directory for each project and
the daemon routes project-scoped requests to that directory's isolated
`.research_plugin/state.sqlite`. The legacy single-repo mode is still available
with `--repo /path/to/research-repo`.

For auto-reload while editing backend code:

```bash
cd /Users/guraltoo/Documents/dev/proj/experiments/Papyrus/research_plugin
python3 scripts/dev_http_reload.py \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

The HTTP launcher uses code from the installed plugin and runs the shared
multi-project backend. Project state is stored in each project directory after
the UI creates or selects that project. Pass `--repo /path/to/research-repo`
only for the legacy single-repo backend. The default legacy store path is:

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
  "version": "0.0005",
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

Every `tool.call` event also carries `sent_chars` (size of the arguments the
agent sent) and `received_chars` (size of the JSON result the agent received,
serialized exactly as the MCP proxy hands it to the model). Unlike `args`/
`result` above — which are summarized and capped — these are the **true** I/O
sizes, so they hold even when the logged `result` is truncated.

## Tool I/O analyzer (debug)

A bounded SQLite ring (`.research_plugin/tool_calls.sqlite`) records the **full**
request and response of recent tool calls — distinct from the activity log,
which summarizes args and caps results. This powers the Debug page: rank tools
by data returned, then read any single call's raw I/O.

```http
GET  /api/debug/tool-calls?minutes=&source=&status=&tool=&limit=200&sort=ts&order=desc
GET  /api/debug/tool-calls/{id}
POST /api/debug/tool-calls/clear
```

The list endpoint filters by time window (`minutes`), `source` (mcp/http/app),
`status` (ok/error), and `tool` (substring), then returns a per-tool aggregate
plus a sorted slice of individual calls. `sort` ∈ `ts | received_chars |
sent_chars | duration_ms | tool`. Call rows are lightweight (sizes + an `id`);
fetch one call's full raw payload — parsed back to native JSON — with the `{id}`
endpoint.

```json
{
  "totals": { "calls": 312, "sent_chars": 48211, "received_chars": 5840220, "error_calls": 7 },
  "coverage": { "calls": 312, "stored": 1500, "oldest_ts": "…", "newest_ts": "…", "capped": false },
  "filter": { "minutes": 60, "source": "mcp", "status": null, "tool": null },
  "by_tool": [
    {
      "tool": "experiment.get_state", "calls": 84, "error_calls": 0,
      "sent_chars": 6720, "received_chars": 4120000,
      "avg_received_chars": 49047, "p50_received_chars": 41000, "p95_received_chars": 88000,
      "max_received_chars": 92110, "avg_sent_chars": 80, "max_sent_chars": 120,
      "avg_duration_ms": 24, "max_duration_ms": 61, "last_ts": "…"
    }
  ],
  "calls": [
    { "id": 9123, "ts": "…", "tool": "experiment.get_state", "source": "mcp",
      "status": "ok", "duration_ms": 31, "sent_chars": 80, "received_chars": 92110, "error_code": "" }
  ]
}
```

`GET /api/debug/tool-calls/{id}` returns the same row plus `args` and `result`
as native JSON (`result` is a plain string for error calls), with
`args_truncated`/`result_truncated` flags for the rare oversized payload (stored
as a `{ "_truncated": true, "_chars": N, "preview": "…" }` marker; the size
fields stay exact). `by_tool` is sorted by `received_chars` descending — the
worst context offenders first. `coverage.capped` is true when the requested
window may extend past calls already evicted from the ring.

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
  "summary": "Evaluate a threshold classifier on a toy dataset.",
  "repo_root": "/absolute/path/to/toy-length-classifier"
}
```

`repo_root` is required in shared backend mode. It is the local directory that
owns the project's files and `.research_plugin` state. `GET /api/projects`
returns `repo_root` for directory-backed projects so the UI can show what each
project owns.

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
and then recency. Each active experiment includes its current `workflow`, its
`sandboxes`, and its active `active_processes`.

`active_processes` contains running sandbox rows (status `running`), plus
`process_type: "sandbox"` and a compact `experiment` summary.

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
DELETE /api/projects/{project_id}/resources/{resource_id}
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
Deleting a resource removes it from active lists and workflow associations, but
keeps observed version metadata; registering the same path again revives the
resource.

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

## Sandboxes

Sandboxes are exposed through HTTP for **observation** only. Procurement is an
agent action (the `sandbox.request` MCP tool); the UI does not provision
sandboxes or run commands. Each experiment has at most one sandbox.

```http
GET  /api/projects/{project_id}/sandboxes
GET  /api/sandboxes/health
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox/metrics
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox/terminal?tail=50000
GET  /api/projects/{project_id}/experiments/{experiment_id}/results/metrics
POST /api/projects/{project_id}/experiments/{experiment_id}/sandbox/sync
POST /api/projects/{project_id}/experiments/{experiment_id}/sandbox/release
```

### Archived results metrics (outlive the VM)

The MLflow tracking server runs *on* the sandbox, so terminating the VM would
take the metrics history with it. The daemon therefore archives a structured
snapshot of MLflow's state (experiments → runs → params, final metric values,
and downsampled per-metric history) to local disk on every sync — throttled in
the auto-sync loop — and force-refreshes it right before release/reap.

`GET .../results/metrics` serves that archive at any time, including long after
the sandbox is terminated:

```json
{
  "experiment_id": "exp_...",
  "available": true,
  "sandbox_status": "terminated",
  "captured_at": "2026-06-11T01:23:45+00:00",
  "source": "mlflow",
  "base_url": "http://127.0.0.1:64382",
  "experiments": [
    {
      "experiment_id": "1",
      "name": "lora_roberta_glue_paper_only",
      "runs": [
        {
          "run_id": "...", "run_name": "...", "status": "RUNNING",
          "start_time": 1765400000000, "end_time": null,
          "params": {"lr": "0.0005"},
          "metrics": {"acc": {"last": 0.91, "step": 20, "timestamp": 1765400001000, "min": 0.85, "max": 0.91}},
          "history": {"acc": [[10, 0.85], [20, 0.91]]}
        }
      ]
    }
  ]
}
```

`available: false` (with a `hint`) means nothing was ever captured — no MLflow
runs existed, or the sandbox predates archiving. `history` arrays are
`[step, value]` pairs downsampled to ≤1000 points; `metrics.*.last` is always
the exact final value. Non-finite values (NaN/Inf) are stored as `null`.

A sandbox row looks like:

```json
{
  "experiment_id": "exp_...",
  "sandbox_id": "sb-...",
  "status": "running",
  "gpu": "A100",
  "cpu": 2.0,
  "memory": 8192,
  "ssh_host": "...", "ssh_port": 50022, "ssh_user": "root",
  "workdir": "/workspace/exp_...",
  "sync_dir": "/workspace/exp_...",
  "sandbox_data_dir": "/workspace/data",
  "local_sync_dir": "/path/to/repo/experiments/exp_...",
  "initial_pushed": 12,
  "dashboards": {
    "mlflow": "https://...modal.host",
    "tensorboard": "https://...modal.host"
  },
  "expires_at": "2026-06-01T18:00:00Z"
}
```

`dashboards` is a `name → URL` map for the in-sandbox observability servers
(MLflow on port 5000, TensorBoard on 6006). Modal entries are public HTTPS URLs
from encrypted tunnels; Lambda Labs entries are daemon-owned loopback URLs backed
by SSH local forwards. The map is always present; expect an empty object when the
backend exposed no dashboards (older rows, fake test backend, or dashboards that
are still installing/starting). Render one tab per non-empty entry as an
`<iframe>`. Treat non-loopback URLs as secret-by-obscurity for now. If provider
tunnels relocate, the row updates on the next `sandbox.get`; local SSH forwards
are recreated by the daemon when needed.

The terminal endpoint returns `{ experiment_id, sandbox_id, status, transcript }`
where `transcript` is the recorded command/output log for the experiment's
sandbox. Fresh sandbox setup pushes the experiment's whole local folder
(`experiments/<name>/`) to `/workspace/<name>` before
returning `status: running` (`initial_pushed` reports how many files made the
trip), so a new remote environment starts with the current local experiment
files. The sync endpoint mirrors `/workspace/<name>` from the live
sandbox/VM back into the local experiment folder with SSH `rsync` (an exact
replica: deletions propagate, local edits are overwritten while the sandbox is
live). The regular sync excludes common heavy files and limits file size;
`artifacts_to_keep/` inside the experiment folder is the deliberate
large-artifact exception path. Everything outside the experiment folder (e.g.
`/workspace/data` for datasets, caches, checkpoints) stays remote and is never
synced. Sandbox telemetry (mlflow.db, TensorBoard events, transcript) is pulled
separately into `.research_plugin/sessions/<experiment_id>/<sandbox_id>/`. The
release endpoint runs a final best-effort sync, then terminates the sandbox and
returns the updated row.

Lambda Labs is the **default** backend (`RESEARCH_PLUGIN_EXECUTION_BACKEND`
unset or `lambda_labs`): sandbox procurement launches a Lambda Labs VM with SSH
and the baseline agent tooling installed via launch `user_data`. The same SSH
rsync path is used for `sandbox.sync`; no provider volume or volume-like storage
is required. Because Lambda machines are fixed GPU+CPU+RAM SKUs, the sandbox row
carries the chosen `instance_type` and `region` alongside `gpu`/`cpu`/`memory`,
and the agent selects one from live availability (see the `needs_selection`
response and `sandbox.options` in MCP_SERVER_CONTRACT.md).

The metrics endpoint returns live in-container usage, sampled on demand inside
the sandbox (CPU/RAM via cgroups, GPU via `nvidia-smi`). It is best-effort:
`available` is `false` (with `metrics: null`) when the sandbox is not running or
the sampler came back empty (e.g. a CPU-only image without `nvidia-smi`). Samples
are coalesced for ~2s so concurrent pollers don't double-exec.

```json
{
  "experiment_id": "exp_...",
  "sandbox_id": "sb-...",
  "status": "running",
  "available": true,
  "sampled_at": "2026-06-02T23:14:00Z",
  "reserved": { "gpu": "A100", "cpu": 2.0, "memory_mib": 8192 },
  "metrics": {
    "cpu": { "used_cores": 1.73, "limit_cores": 2.0 },
    "memory": { "used_bytes": 2147483648, "limit_bytes": 8589934592 },
    "gpus": [
      { "index": 0, "name": "NVIDIA A100-SXM4-40GB",
        "util_pct": 37, "mem_used_mib": 512, "mem_total_mib": 40960 }
    ]
  }
}
```

## Events

```http
GET /api/projects/{project_id}/events?limit=100
```

Returns recent accepted state events. Useful for a compact timeline or debug
drawer.
