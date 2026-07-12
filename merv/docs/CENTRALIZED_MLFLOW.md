# Centralized MLflow

MLflow is the quantitative ledger for Merv projects. The brain owns
workflow state, claims, reviews, resource records, and logic graphs. MLflow owns
the empirical run record: parameters, metrics and their histories, run tags,
datasets recorded through MLflow, and run artifacts.

All runs for one brain deployment use a shared MLflow service. Merv
names the MLflow experiment for a plugin experiment:

```text
rp/<project_id>/<experiment_id>
```

The plugin stores compact run metadata on the experiment record: run id/name,
status, artifact URI, creation time, and last error. It exposes bounded
compatibility views on demand, but does not mirror MLflow's database. Agents use
MLflow's native APIs for run search, comparison, metric history, and artifact
access.

## Runtime topology

```text
local execution or remote sandbox
  -> RESEARCH_PLUGIN_MLFLOW_TRACKING_URI
  -> MLflow tracking and artifact service

brain
  -> RESEARCH_PLUGIN_MLFLOW_SERVER_URI
  -> run creation, finalization, health checks, and compact UI reads
```

`RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` must be reachable from every place that
runs experiments. For a hosted deployment this normally means a public HTTPS
URL. A Docker service name such as `http://mlflow:5000` is suitable for the
brain's internal `SERVER_URI`, but not for agents or remote sandboxes.

The hosted Compose stack runs MLflow beside the brain, with Postgres for MLflow
metadata and an S3-compatible bucket for artifacts. Its recommended ingress
layout is:

```text
https://backend.example.com/mlflow -> MLflow
https://backend.example.com         -> brain
```

For that path layout in the reference Compose stack, set
`RESEARCH_PLUGIN_MLFLOW_STATIC_PREFIX=/mlflow`; Compose passes it to MLflow's
`--static-prefix` flag. The ingress must:

- strip `/mlflow` for MLflow API routes such as `/mlflow/api/*`;
- preserve `/mlflow` for the UI and static assets; and
- rewrite `/mlflow/ajax-api/*` to MLflow's root-mounted `/api/*` handlers.

The shipped localhost brain does not automatically start an MLflow process.
Without explicit MLflow endpoint configuration, `mlflow.context` reports
`configured: false`. To use MLflow with a local brain, run or select an MLflow
service and configure the same endpoint variables. A loopback tracking URL works
for local execution only; remote sandboxes need a URL they can reach directly.

## Configuration

Typical hosted configuration:

```bash
RESEARCH_PLUGIN_MLFLOW_MODE=external
RESEARCH_PLUGIN_MLFLOW_TRACKING_URI=https://backend.example.com/mlflow
RESEARCH_PLUGIN_MLFLOW_SERVER_URI=http://mlflow:5000
RESEARCH_PLUGIN_MLFLOW_DASHBOARD_URL=https://backend.example.com/mlflow
```

- `TRACKING_URI` is returned to agents and training code.
- `SERVER_URI` is the optional brain-internal read/write endpoint.
- `DASHBOARD_URL` is the browser URL; it defaults to `TRACKING_URI`.
- `MODE=external` records that the brain uses a separately operated MLflow
  service.

`SERVER_URI` alone lets the brain read MLflow for compatibility views, but it
does not configure agent logging. `TRACKING_URI` alone gives agents a logging
endpoint, but the brain cannot pre-create or finalize a canonical run because
those writes require `SERVER_URI`. Configure both to use the complete workflow.

Hosted deployments can set:

```bash
RESEARCH_PLUGIN_REQUIRE_AGENT_MLFLOW=1
```

This makes brain startup fail when `TRACKING_URI` is empty. It does not probe
that URL for reachability, and it does not make MLflow evidence an experiment
workflow gate.

## Agent contract

The stdio proxy resolves the linked project and injects its `project_id`; agents
do not pass project scope themselves. Use:

```text
mlflow.context()
mlflow.context(experiment_id="exp_...")
```

Project scope returns the tracking URI, dashboard URL, namespace prefix, and a
map from plugin experiments to MLflow experiment names. Experiment scope also
returns the exact experiment name and environment variables for a run:

```json
{
  "scope": "experiment",
  "project_id": "proj_123",
  "experiment_id": "exp_456",
  "mlflow": {
    "configured": true,
    "mode": "external",
    "tracking_uri": "https://backend.example.com/mlflow",
    "experiment_name": "rp/proj_123/exp_456",
    "dashboard_url": "https://backend.example.com/mlflow",
    "env": {
      "MLFLOW_TRACKING_URI": "https://backend.example.com/mlflow",
      "MLFLOW_EXPERIMENT_NAME": "rp/proj_123/exp_456",
      "RP_PROJECT_ID": "proj_123",
      "RP_EXPERIMENT_ID": "exp_456"
    }
  }
}
```

`experiment.transition(transition="start_running")` returns the same
experiment-scoped block. When both `TRACKING_URI` and `SERVER_URI` are
configured, the brain makes a best-effort attempt to create an initial MLflow
run and persist its identity on the experiment. On success, the response
includes `mlflow.run.run_id`, and the environment includes `MLFLOW_RUN_ID` and
`RP_MLFLOW_RUN_ID`.

Set the returned variables on the command that starts training. If a run id is
present, resume it with MLflow's native API, for example:

```python
import os

import mlflow

mlflow.start_run(run_id=os.environ["MLFLOW_RUN_ID"])
```

Do not rely on ambient shell state and do not create a file-backed MLflow store
as a fallback for a Merv experiment. Remote sandboxes are clients of
the configured central service; sandbox provisioning does not start MLflow or
create a tunnel.

An infrastructure retry stays on the same plugin attempt. If the persisted
MLflow run is still open, `retry_running` returns it for resumption. If that run
is terminal, the brain attempts to create and persist a fresh run for the same
attempt.

## Finalizing a quantitative run

After a quantitative command finishes, call:

```text
mlflow.finalize_run(experiment_id="exp_...")
```

By default the tool uses the plugin-owned run id, requests `FINISHED` through
the brain's `SERVER_URI`, and briefly polls MLflow so experiment state does not
retain a stale `RUNNING` value. Use `status="FAILED"` or `status="KILLED"` for
an unsuccessful run. Use `status=null` when the training script already closed
the run and only readback is needed.

Finalization reads before writing. A run that is already terminal keeps its
recorded status, so the default cannot overwrite a script-recorded failure with
`FINISHED`. Passing an explicit run id can finalize that run, but it does not
replace a different canonical run already stored on the plugin experiment.

## Quantitative run metadata

MLflow is expected for training, evaluation, sweeps, ablations, and other work
where metrics drive the conclusion. It is not required for qualitative
experiments, literature work, code-only probes, or planning.

Every quantitative run should identify:

```text
project_id
experiment_id
run purpose or run group
primary_metric, when one is defined
primary_metric_direction, when one is defined
execution backend or sandbox id, when useful
```

A brain-created run already carries `project_id`, `experiment_id`,
`attempt_index`, and `created_by=research_plugin` tags. Agents may add run
purpose, metric direction, backend, dataset, and configuration metadata.

Keep compact plots, tables, evaluation JSON, prediction samples, confusion
matrices, and resolved configuration as MLflow artifacts when they help explain
the run. Keep workflow-facing summaries and selected figures as checkout
resources. Large datasets and model files can use durable object storage. These
are separate records; a repo resource is still a checkout file, not a pointer
to an MLflow or storage object.

## Metrics exhibit

During a running experiment, `experiment.exhibit` previews the system metrics
exhibit. At `submit_results`, the brain regenerates and pins it when
attempt-window runs are found, or when MLflow is unavailable after a
plugin-created run established quantitative intent. It includes the
attempt-window MLflow runs and eligible pinned result JSON, with provenance for
each entry. Qualitative/no-run attempts receive no pinned exhibit. When an
exhibit is pinned, the report must reference and interpret it. Runs written
after `submit_results` remain in MLflow but are outside that attempt's finalized
exhibit. Compatibility reads are bounded to the newest 50 runs; when that limit
is reached, the exhibit records the cap rather than claiming an uncapped
history.

## UI compatibility views

The brain exposes bounded MLflow views for the UI:

```text
GET /api/projects/{project_id}/mlflow
GET /api/projects/{project_id}/experiments/{experiment_id}/results/metrics
```

They include recent runs, parameters, final metric values, downsampled metric
histories, dashboard links, and project-level MLflow health/configuration. The
experiment metrics snapshot omits the queried MLflow base URL. These views are
not a second quantitative ledger or an agent query language; the durable run
record remains in MLflow.

## Failure behavior

MLflow is best-effort in the experiment workflow:

- An unconfigured service is visible in MLflow context. Context itself derives
  `configured` from URI presence and does not contact MLflow. Network access
  occurs during initial/retry run creation, health checks, finalization, and
  compatibility reads.
- Experiment transitions do not gate on MLflow availability.
- `RESEARCH_PLUGIN_REQUIRE_AGENT_MLFLOW=1` separately makes brain startup fail
  when `TRACKING_URI` is absent.
- A quantitative run without usable MLflow should retain fallback result files
  in the experiment folder and explain the gap in its report.
