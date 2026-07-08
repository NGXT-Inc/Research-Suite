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

Run the localhost brain:

```bash
research_plugin/bin/research-plugin-http --host 127.0.0.1 --port 8787
```

The HTTP process is a control brain. Local file reads and writes are performed
by the stdio MCP proxy and submitted through the data-plane endpoints.

For auto-reload while editing backend code:

```bash
cd /path/to/research-suite/research_plugin
python3 scripts/dev_http_reload.py \
  --host 127.0.0.1 \
  --port 8787 \
  --activity-stderr
```

The HTTP launcher uses code from the installed plugin and stores brain state
under the configured local state directory.

Activity is stored beside the brain state DB:

```text
/path/to/state/.research_plugin/activity.jsonl
```

The UI can read recent activity through `GET /api/activity?limit=100`. For live
terminal visibility of MCP tool-call activity, use:

```bash
tail -f /path/to/state/.research_plugin/activity.jsonl
```

## Principles

- UI reads project state from HTTP.
- Codex and reviewer agents use MCP.
- Both HTTP and MCP share the same SQLite state and service layer.
- UI may create claims, experiments, resources, transitions, and reviews, but it
  should not include an agent chat surface in this version.
- Resources are local repo files observed by the MCP proxy. Registering a
  resource stores a pointer and observed file metadata; it does not upload
  bytes.
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
  "version": "0.0011"
}
```

## Activity

```http
GET /api/activity?limit=100
```

Returns recent backend activity events. Events are MCP tool calls. Tool-call
arguments are summarized to IDs and workflow fields. Successful tool-call events
include a capped, JSON-safe tool `result`; sensitive fields such as
`reviewer_capability` and `capability` are redacted before persistence.

In hosted control mode, activity reads are scoped to projects owned by the
authenticated principal. Supplying a foreign `project_id` returns not-found;
unscoped reads include only project-attributed events for the caller's visible
projects.

```json
{
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

In hosted control mode the debug surface is tenant-scoped: list/detail/clear
only see calls for projects owned by the authenticated principal. Capability-like
fields (`reviewer_capability`, `capability`) are redacted before persistence,
and hosted responses strip local data-plane fields such as `repo_root` and
`local_sync_dir`.

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

Project creation never carries a repo path over HTTP. Folder-to-project links
are local MCP proxy state.

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
- `retry_running`
- `submit_results`
- `complete`
- `mark_failed`
- `abandon`

`retry_running` is only valid from `running`. It keeps the same status and
attempt index, and uses `revision_context` to tell the agent that the previous
execution was interrupted by infrastructure rather than rejected as a plan
change.

The UI should generally use `workflow.next_action` from status/home to decide
which action button to show.

Review gates stay as user-facing stages such as `design_review` and
`experiment_review`. When present, `workflow.review_gate` gives the substate:

- `none`: Needs reviewer.
- `requested`: Reviewer pending.
- `started`: Reviewer active.

The UI should render this as detail inside the review stage, not as a separate
top-level stage.

Experiment state also includes `storage_objects`: compact references to
non-deleted durable storage objects whose `producing_experiment_id` matches the
experiment. Use these for large retained artifacts such as checkpoints, logs,
or datasets that should be visible to reviewers but are not repo resources.
Once an experiment is `running` or later, experiment state also includes
`mlflow`, the central tracking context and dashboard link for that experiment.
If `start_running` created a backend-owned MLflow run, state also includes
`mlflow_run`; the `mlflow` block nests it as `mlflow.run` and adds
`MLFLOW_RUN_ID` / `RP_MLFLOW_RUN_ID` to `mlflow.env` for resume-in-place
logging. `mlflow.finalize_run` refreshes that same `mlflow_run` object after
execution so UI state does not keep showing a stale immediate `RUNNING` status
once the backend readback sees a terminal MLflow run.
Completed experiments with tested claims and a conclusion include
`claim_update_suggestions`, a list of scoped `claim.update` call skeletons that
the UI can show as follow-up actions after review.

## Syntheses

Project reflection waves. Each wave reconciles five lens reflections into the
living project logic graph (role `project_graph`), a concise reflection document (role
`reflection_doc`; legacy waves may show `synthesis_doc`), and a change spec
(role `change_spec`), gated by a synthesis review before publish (see the
`project-reflection` skill).

```http
GET /api/projects/{project_id}/reflections
GET /api/projects/{project_id}/reflections/{synthesis_id}
GET /api/projects/{project_id}/reflections/current/graph
GET /api/projects/{project_id}/reflections/{synthesis_id}/graph
```

These `/reflections*` paths are canonical. The former `/syntheses*` aliases
have been removed now that the UI is migrated. The payload keys are still
synthesis-named (`syntheses`, `open_synthesis`, `synthesis_id`) — a separate
body rename, not part of this path migration.

`GET /reflections` returns the whole history in one call (each entry is a full
wave state, so the UI drives the panel off this alone):

```json
{
  "syntheses": [ /* full wave states, oldest-first */ ],
  "current": { /* open wave, else latest published, else null */ },
  "open_synthesis": { /* the one non-terminal wave, or null */ },
  "latest_published": { /* or null */ },
  "signal": { /* staleness/coverage: hint, terminal_experiments, covered_terminal_experiments, last_published_at */ }
}
```

Each wave state (also returned by `/reflections/{synthesis_id}`) carries:

- `id`, `title`, `status` (`reflecting` → `synthesizing` → `synthesis_review` →
  `published`; `abandoned` is terminal), `attempt_index`, `revision_context`
  (set when a review sent the wave back), `created_at`, `published_at`;
- `roster`: the five lenses, each `{ id, title, charter, core, why_distinct }`
  (three core — `amplify`, `avoid`, `entropy` — plus two authored);
- `corpus`: the snapshot of terminal experiments + claims the wave covers;
- `resources` / `current_attempt_resources`: associated files, each with
  `association_role` (`reflection_lens_doc` | `project_graph` |
  `reflection_doc` | `change_spec`; legacy `reflection` | `graph` |
  `synthesis_doc`),
  `association_attempt_index`, `association_version_id`, `path`, `id`;
- `reviews`: synthesis reviews, each with `verdict`, `return_to`
  (`reflecting` | `synthesizing`), `notes`, `findings`, `evidence`, `role`,
  `attempt_index`, `created_at`;
- `reflection_coverage`: `{ lenses: [{ lens_id, covered, path, version_id }],
  missing, complete }` — per-lens reflection coverage for the current attempt;
- `gate_checklist`: the current forward gate as checklist data. `reflecting`
  lists one `reflection_lens:<lens_id>` item per roster lens; `synthesizing`
  lists the `project_graph`, `reflection_doc`, and `change_spec` resources with
  missing/valid/invalid status and validator `problems`; `synthesis_review`
  lists the `review:reflection_reviewer` item with pending/requested/started/
  passed status;
- `project_graph_diff`: previous-published-vs-current graph comparison when a
  current project graph is associated. The block includes `available`,
  base/current reflection and graph version ids, a summary, and node/edge
  `added`, `removed`, `changed`, and `unchanged_count` groups;
- `allowed_transitions`.

`GET /reflections/current/graph` renders the open wave's project graph (else the
latest published one); `GET /reflections/{synthesis_id}/graph` renders that
specific wave's graph from the bytes it pinned, so a past wave shows faithfully
even after a later wave overwrites the living `project/logic_graph.json`. Both
share the per-experiment graph payload shape — `{ available, graph, problems,
max_nodes, ref_index, path, synthesis }` — and the `current` endpoint also
includes `signal`. Lens reflection doc, reflection document, and change spec file
content is read through the Resources `/content` endpoint, with `?version=`
for faithful historical waves. Relative image links in reflection documents use
the same Resources `/file?rel=...` endpoint as report figures; submitted image
bytes are served from the blob store when available.

## Resources

```http
GET /api/projects/{project_id}/resources
GET /api/projects/{project_id}/resources?kind=result
GET /api/projects/{project_id}/resources/tree
GET /api/projects/{project_id}/resources/{resource_id}
GET /api/projects/{project_id}/resources/{resource_id}/history
GET /api/projects/{project_id}/resources/{resource_id}/content
GET /api/projects/{project_id}/resources/{resource_id}/content?version={version_id}
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
the exact `version_id` associated to that attempt. Without `?version=`, `/content`
serves the latest submitted bytes for gated roles (plan/report/graph/
reflection_doc/change_spec/reflection; legacy synthesis_doc) and the current
live file for other roles; `/file` reads the current live file. "Latest
submitted bytes" resolves to the resource's `current_version_id` — so a living
file that several targets pin (e.g. `project/reflection.md` across reflection
waves) serves its newest version, not whichever association carries the highest
per-target attempt index. Passing
`?version={version_id}`
serves the exact submitted bytes of that version from the blob store — the
version must be associated to the resource (else 404), and a missing or
undecodable blob degrades to
`{ "available": false, "reason": "version_unavailable" }`. This is how the
reflection-wave UI renders a past wave's reflection artifacts faithfully (the
living files have since moved on). `history` returns version metadata (sha256,
size, mtime, content_type) only. Deleting a resource removes it from active
lists and workflow associations, but keeps observed version metadata;
registering the same path again revives the resource.

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
sandboxes or run commands. A sandbox can be attached to multiple active
experiments, and an experiment can have multiple live sandboxes.

```http
GET  /api/projects/{project_id}/sandboxes
GET  /api/sandboxes/health
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox/metrics
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox/terminal?tail=50000
GET  /api/projects/{project_id}/experiments/{experiment_id}/results/metrics
GET  /api/projects/{project_id}/mlflow
POST /api/projects/{project_id}/experiments/{experiment_id}/sandbox/release
```

### MLflow results metrics view

MLflow tracking is backend-owned and is the quantitative ledger. The plugin
does not own a second durable copy of MLflow. `GET .../results/metrics` is a
compact compatibility view over the centralized MLflow experiment: runs, params,
final metric values, and downsampled per-metric history.

Agents should prefer direct MLflow programmatic access for serious read, sort,
filter, comparison, metric-history, and artifact-download work. This endpoint
exists for UI panels and lightweight summaries.

`GET .../results/metrics` returns the current bounded view when MLflow is
configured and reachable:

```json
{
  "experiment_id": "exp_...",
  "available": true,
  "source": "mlflow",
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

`available: false` with a `hint` means no matching MLflow runs were found, MLflow
is not configured, or MLflow is unavailable. `history` arrays are `[step, value]`
pairs downsampled to at most 1000 points; `metrics.*.last` is the latest value
reported by MLflow. Non-finite values (NaN/Inf) are returned as `null`.

`GET /api/projects/{project_id}/mlflow` returns the MLflow health/context block
plus one compact metrics view per plugin experiment, with dashboard deep links
when the MLflow experiment id can be resolved. It is a project-scoped navigation
page for humans, not the primary agent query surface.

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
  "expires_at": "2026-06-01T18:00:00Z"
}
```

Sandbox rows do not expose sandbox-local dashboards. Centralized MLflow is
backend-owned and exposed through the project MLflow endpoints and
`mlflow.context` tool responses, not through sandbox tunnels.

The terminal endpoint returns `{ experiment_id, sandbox_id, status, transcript }`
where `transcript` is the recorded command/output log for the sandbox. Fresh
sandbox setup returns SSH details and a remote work folder. No files are copied
automatically: agents fetch code/data on the box and explicitly retain outputs
before release by copying light files over SSH or uploading heavy artifacts with
storage tools. Everything left on the VM at release or expiry is destroyed.

Lambda Labs is the **default** backend (`RESEARCH_PLUGIN_EXECUTION_BACKEND`
unset or `lambda_labs`): sandbox procurement launches a Lambda Cloud VM with SSH
and installs the baseline agent tooling over the management SSH channel. Lambda
Labs and Thunder Compute both use fixed GPU+CPU+RAM specs, so the sandbox row carries the
chosen `instance_type` alongside `gpu`/`cpu`/`memory`, and the agent selects one
from live availability (see the `needs_selection` response and `sandbox.options`
in MCP_SERVER_CONTRACT.md).

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
