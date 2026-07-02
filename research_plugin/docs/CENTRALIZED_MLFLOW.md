# Centralized MLflow

**Status:** implemented  
**Updated:** 2026-06-30

MLflow is the quantitative ledger for Research Plugin projects. The plugin
keeps the workflow, claims, reviews, resources, and logic graph; MLflow keeps
the empirical run record: params, metrics, metric histories, artifacts, dataset
lineage, and model outputs.

New runs log to one centralized MLflow tracking service for the backend
deployment. Sandboxes and local executions are MLflow clients only.

The plugin's MLflow bridge should stay small. Its job is to tell agents where
MLflow is, which experiment namespace to use, which tags are required, and which
dashboard links help humans inspect the ledger. Agents should use MLflow's own
programmatic APIs for read, sort, filter, comparison, metric history, and
artifact download.

## Decisions

- Run one MLflow server per backend deployment, not one per project.
- Namespace runs as `rp/<project_id>/<experiment_id>`.
- Keep stable IDs in MLflow names; human names belong in tags.
- Treat MLflow as the primary quantitative source of truth. Do not mirror the
  full MLflow database into plugin state.
- Keep plugin-side MLflow reads as compatibility/context helpers, not as the
  preferred agent navigation surface.
- Store conclusions, claim relationships, reviews, and curated resource links in
  plugin state; store raw quantitative run records and artifacts in MLflow.
- Add auth later at the same endpoint/env-injection boundary.

## Deployment Modes

### Hosted / Remote Backend

The backend VM runs MLflow beside the control server.

```text
remote sandbox -> RESEARCH_PLUGIN_MLFLOW_TRACKING_URI / public MLflow URL
backend        -> RESEARCH_PLUGIN_MLFLOW_SERVER_URI / internal MLflow URL
```

The recommended hosted ingress is to keep MLflow parallel to the control app and
route it through the same public HTTPS host, for example:

```text
https://backend.example.com/mlflow -> MLflow
https://backend.example.com         -> control
```

When using this path layout, set `RESEARCH_PLUGIN_MLFLOW_STATIC_PREFIX=/mlflow`
for the MLflow container so the UI and static assets are generated under the
same prefix. At the ingress layer, strip `/mlflow` only for MLflow API routes
such as `/mlflow/api/*`, preserve `/mlflow` for the UI/static routes, and
rewrite `/mlflow/ajax-api/*` to MLflow's root-mounted `/api/*` handlers. The
browser bundle uses the AJAX path when loading experiment and run data.

The compose stack starts:

- `control`
- `mlflow`
- Postgres databases for backend state and MLflow
- MinIO buckets for backend blobs and MLflow artifacts

### Local Backend

If no external MLflow URI is configured, the local HTTP backend starts one
managed MLflow process under the registry state directory.

Local processes use the local URL directly:

```text
MLFLOW_TRACKING_URI=http://127.0.0.1:<port>
```

Remote sandboxes are MLflow clients. They publish to the configured tracking
URI directly; sandbox access does not create MLflow tunnels or sandbox-local
MLflow servers.

## Configuration

```bash
RESEARCH_PLUGIN_MLFLOW_MODE=external
RESEARCH_PLUGIN_MLFLOW_TRACKING_URI=https://backend.example.com/mlflow
RESEARCH_PLUGIN_MLFLOW_SERVER_URI=http://mlflow:5000
RESEARCH_PLUGIN_MLFLOW_DASHBOARD_URL=https://backend.example.com/mlflow
```

- `TRACKING_URI`: what agents/training code use.
- `SERVER_URI`: optional backend-internal route for health checks and
  compatibility reads.
- `DASHBOARD_URL`: what users open.
- `managed`: local backend starts MLflow itself.
- `external`: backend points at an existing MLflow service.

`TRACKING_URI` and `SERVER_URI` intentionally mean different things in hosted
control mode. `SERVER_URI` may be an internal service name such as
`http://mlflow:5000`; the control plane can use it to read metrics, but remote
sandboxes cannot. Agents only receive `MLFLOW_TRACKING_URI` when
`RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` is set to a URL reachable from the run
location, usually the public HTTPS MLflow endpoint. A deployment with only
`SERVER_URI` configured can still show backend-read metrics, but agent logging is
reported as unconfigured until `TRACKING_URI` is supplied.

## Agent Contract

`mlflow.context` is the agent-facing MLflow bridge. With only `project_id`, it
returns the project-level tracking URI, dashboard URL, namespace prefix, and the
plugin experiment-to-MLflow-name map needed to browse runs with MLflow's native
APIs. With `experiment_id`, it also returns the concrete MLflow experiment name
and env vars for a quantitative run:

```json
{
  "scope": "experiment",
  "project_id": "proj_123",
  "experiment_id": "exp_456",
  "mlflow": {
    "configured": true,
    "tracking_uri": "https://backend.example.com/mlflow",
    "experiment_name": "rp/proj_123/exp_456",
    "dashboard_url": "https://backend.example.com/mlflow",
    "env": {
      "MLFLOW_TRACKING_URI": "https://backend.example.com/mlflow",
      "MLFLOW_EXPERIMENT_NAME": "rp/proj_123/exp_456"
    }
  }
}
```

`experiment.transition(start_running)` also returns the same experiment-scoped
MLflow block as a convenience when a run begins. When
`RESEARCH_PLUGIN_MLFLOW_SERVER_URI` is configured, the control plane also
creates an initial MLflow run and persists its identity on the experiment. The
returned block then includes `mlflow.run.run_id`, and `mlflow.env` includes
`MLFLOW_RUN_ID` / `RP_MLFLOW_RUN_ID` so an agent can resume that run rather
than accidentally creating a sibling run. After that point, `mlflow.context`,
`experiment.get_state`, and the HTTP experiment state endpoint keep surfacing
the experiment-scoped block and run identity so agents and the UI do not need
to rediscover the tracking namespace.

Agents should use those env vars for quantitative experiments, whether running
locally or inside a sandbox. If `MLFLOW_RUN_ID` is present, resume it with
MLflow's native API (for example `mlflow.start_run(run_id=...)`) before logging.
They should not start MLflow servers in sandboxes. The plugin does not rely on
ambient shell state for this: a local agent or an SSH-driven sandbox run must
read this block from MCP and set the returned env vars on the command that
starts training. If `MLFLOW_TRACKING_URI` is absent from the current shell, call
`mlflow.context`; do not fall back to a file-backed local MLflow store for a
Research Plugin experiment.

### Quantitative Run Metadata V0

MLflow is expected only for quantitative work: training, evaluation, sweeps,
ablations, or any run where metrics drive the conclusion. It is not required
for qualitative experiments, literature work, code-only probes, or planning
tasks.

For now, the ledger contract is deliberately small and soft. Every quantitative
run should log enough metadata for an agent to connect the MLflow run back to
the plugin experiment and understand what the run was trying to measure:

```text
project_id
experiment_id
run purpose or run group
primary_metric, when there is a clear primary metric
primary_metric_direction, when there is a clear primary metric
execution backend or sandbox id, when readily available
```

When the plugin-created run is available, it already has `project_id`,
`experiment_id`, `attempt_index`, and `created_by=research_plugin` tags. Agents
may add run-purpose, metric-direction, backend, dataset, and config metadata to
that same run as the experiment executes.

Agents may log lightweight dataset or config notes when they are obvious and
useful, but dataset digests, dataset versioning, config hashes, git metadata,
and claim ids are not part of the V0 requirement. Claims remain traceable
through the plugin experiment record.

Agents should also log compact artifacts that are useful in reports and reviews:
plots, tables, evaluation JSON, prediction samples, confusion matrices, and
resolved configs. Heavy artifacts should remain in durable object storage or
MLflow artifact storage, with plugin resources pointing to the curated evidence
that supports the workflow conclusion.

### Direct MLflow Reads

Agents are expected to use MLflow directly for quantitative navigation. Typical
read tasks include:

```text
search runs by params, tags, status, and metric thresholds
sort runs by the declared primary metric
fetch metric histories for plotting
list and download run artifacts
compare runs across seeds, ablations, or datasets
retrieve run tags and dataset metadata
```

The plugin should not grow a second query language for MLflow. New custom tools
should be added only when they supply plugin context that MLflow does not know,
such as the current project id, experiment id, namespace mapping, or dashboard
links.

## UI Compatibility Views

The `/results/metrics` endpoint and project MLflow page expose bounded
plugin-side views of MLflow data for UI compatibility. They are intentionally
compact: recent runs, params, final metric values, and downsampled metric
histories. Both feed one shared renderer: an inline per-experiment panel on the
experiment detail page and the project-wide MLflow page. The service owns the
`dashboard_experiment_url` deep link (namespace → `#/experiments/<id>`) so UI
surfaces never reconstruct MLflow routes themselves.

These views are not the quantitative ledger. They should not be extended into a
full MLflow mirror or agent query layer. The durable record is the centralized
MLflow backend and its artifact store. Compatibility views should strip internal
server URLs before UI/API exposure.

## Failure Policy

MLflow is best-effort for now:

- If configured and reachable, inject the bridge block and let agents log to the
  central tracking server.
- If unreachable, report readiness in experiment MLflow helpers and health output.
- Training is not blocked solely because MLflow is down, but quantitative
  reports should state the failure and preserve fallback result files under the
  experiment folder.
- Once MLflow is treated as a hard gate for a project, completion should require
  MLflow run IDs or an explicit fallback evidence bundle.

Future user auth should scope the same environment injection point. Do not point
`RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` at a Docker-internal hostname unless all
training clients run on that same network.
