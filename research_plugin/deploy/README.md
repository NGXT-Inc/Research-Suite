# Research Plugin — control-plane reference deploy

This directory is the **reference stack** for running the Research Plugin cloud
control plane (cloud backend migration, Phase 9). It is for development, testing,
and as a worked example — **it is not a managed deploy**. TLS, managed Postgres,
a real secret store, autoscaling, backups, and the cleanup scheduler are the
operator's responsibility (see "What this stack does NOT do" below).

## What's here

| File | Purpose |
|---|---|
| `Dockerfile` | Control-plane image: installs the `control` extra, runs `research-plugin-control` (uvicorn) as a non-root user, with a `HEALTHCHECK` hitting `/api/meta`. |
| `Dockerfile.mlflow` | Separate MLflow server image for the reference compose stack. |
| `docker-compose.yml` | Full local stack: control + MLflow + Postgres (record stores) + MinIO (S3-shape blob/storage/artifact stores), with one-shot bucket/database creators. |
| `.env.example` | Documents the control-mode environment (§3.4 config matrix). Copy, fill, and keep out of version control. |
| `.dockerignore` | Keeps secrets, local state, the React UI, and tests out of the build context. |

## Quick start (local full stack)

```sh
# From the plugin root (research_plugin/):
docker compose -f deploy/docker-compose.yml up --build
```

This brings up:
- **Postgres** on `localhost:5432` — the record store (Postgres dialect).
- **MLflow** on `localhost:5000` — centralized tracking server. It uses a
  separate `mlflow` Postgres database and the `research-plugin-mlflow-artifacts`
  MinIO bucket. This loopback URL is for local browser/dev access only; remote
  sandboxes need `RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` set to the public HTTPS
  MLflow URL.
- **MinIO** on `localhost:9000` (console `:9001`) — the S3-shape blob and
  heavy-file storage backend; the `createbucket` job makes the Research Plugin
  blob bucket, storage bucket, and MLflow artifact bucket.
- **mgmtkey** one-shot — creates a dev-only management SSH key in a named volume
  and mounts it read-only into control. Managed deploys should use a real secret
  manager instead.
- **control** on `localhost:8787` — private control API, daemon
  task/sync-target endpoints ON, reaper ON.

Verify the control plane is up and learn the version floor:

```sh
curl -s http://localhost:8787/api/meta
# {"server_version":"...","min_daemon_version":"...","min_proxy_version":"..."}
```

The current operator-run setup is a private control plane. Client VMs run
`research-plugin-client configure --control-url ...` without any control-plane
token. Put the service behind trusted network boundaries until the real auth
system lands.

## Modes & environment (§3.4)

The same image runs every mode; the entrypoint forces `control`. Key
control-mode variables (full list in `.env.example`):

| Variable | Required | Meaning |
|---|---|---|
| `RESEARCH_PLUGIN_MODE` | yes (forced) | `control` |
| `RESEARCH_PLUGIN_ALLOWED_ORIGINS` | prod | comma-separated hosted UI origins allowed by CORS |
| `RESEARCH_PLUGIN_DB_URL` | prod | `postgres://…` (else SQLite — dev only) |
| `RESEARCH_PLUGIN_BLOB_BUCKET` + `AWS_*` | prod | object store; presign must be a reachable HTTPS PUT |
| `RESEARCH_PLUGIN_MLFLOW_MODE` + `RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` | prod | centralized MLflow endpoint reported to training clients |
| `RESEARCH_PLUGIN_MLFLOW_SERVER_URI` | optional | backend-internal MLflow URL for metrics reads when it differs from the client URL |
| `RESEARCH_PLUGIN_MLFLOW_DASHBOARD_URL` | optional | human-facing MLflow UI URL when it differs from the tracking URI |
| `RESEARCH_PLUGIN_MGMT_KEY_PATH` + `RESEARCH_PLUGIN_MGMT_PUBLIC_KEY` | prod | mounted management SSH key; readable by control, 0600/0400, rotated by drain/restart |
| `THUNDER_COMPUTE_API_KEY` / `MODAL_*` / `LAMBDA_API_KEY` / `HF_TOKEN` | to provision | provider creds — **secret store only** in control mode (`.env` discovery is disabled); `HF_TOKEN` is delivered post-boot over the management channel, never embedded in VM `user_data` |

The production control entrypoint runs without a checkout/staging repo. Startup
therefore fails fast unless the durable DB, durable blob store, and mounted
management key variables are present. Passing an explicit `repo_root` is only
for dev/test compatibility.

For MLflow, `RESEARCH_PLUGIN_MLFLOW_SERVER_URI` alone is enough for the control
plane to read metrics from an internal service, but it is not enough for agents
to log runs. Set `RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` to the public HTTPS URL
reachable by every run location — local client machines and remote sandboxes —
before expecting training code to emit MLflow runs. Agents retrieve that URL
through `mlflow.context`; sandbox provisioning does not inject it by itself.
When serving MLflow through the same host as the control plane, use a path such
as `https://backend.example.com/mlflow` and set
`RESEARCH_PLUGIN_MLFLOW_STATIC_PREFIX=/mlflow` so MLflow generates UI/static
links under that prefix.

## Operating

- **Version floor:** clients send `X-RP-Client-Version`; below-floor clients get
  a `426` with an upgrade message. Floors are constants in `backend/version.py`.
- **Cleanup jobs:** the control plane BUILDS the cleanup sweeps (orphan VMs,
  blob TTL GC, lease expiry, stale-provision reap) but does **not** schedule
  them — POST `/api/admin/cleanup` from a managed cron / sidecar tick on your
  cadence. `run_all` is idempotent and clock-injectable.
- **Spend kill-switch:** a tripped per-tenant or global kill-switch refuses new
  provisioning; budgets (GPU-hours / USD) are reconstructed from the generation
  ledger. See `QuotaService`.
- **Observability:** the control plane emits one redacted JSON log line per HTTP
  request to stdout (request id + tenant id + path + status + duration). Run
  with `PYTHONUNBUFFERED=1` (the image sets it) so your log pipeline sees them.
- **Per-tenant counters:** GET `/api/admin/tenants/{tenant_id}/counters`.

## TLS termination

The control plane serves plain HTTP on `:8787`. Put it **behind a
TLS-terminating load balancer / reverse proxy** (ALB, nginx, Caddy, Traefik) in
production — the daemon and proxy must dial `https://`. `/health` and `/api/meta`
are open for liveness/handshake. All other routes are currently private
operator/admin routes, not public internet routes.

For the reference single-host deployment, keep MLflow parallel to the control
app and route it at the ingress layer instead of opening port `5000` publicly:

```caddy
backend.example.com {
  handle /mlflow/health {
    uri strip_prefix /mlflow
    reverse_proxy 127.0.0.1:5000
  }

  handle /mlflow/api/* {
    uri strip_prefix /mlflow
    reverse_proxy 127.0.0.1:5000
  }

  handle /mlflow/ajax-api/* {
    uri strip_prefix /mlflow
    reverse_proxy 127.0.0.1:5000
  }

  handle /mlflow* {
    reverse_proxy 127.0.0.1:5000
  }

  handle {
    reverse_proxy 127.0.0.1:8787
  }
}
```

This split is intentional: MLflow's tracking and artifact APIs stay mounted at
the server root, so ingress strips `/mlflow` for API routes. The MLflow UI is
served under the static prefix, so ingress preserves `/mlflow` for UI/static
routes.

## What this stack does NOT do (documented seams)

These are intentionally out of the reference stack — production owns them:

- **TLS certificates** — terminate at your LB; this image speaks HTTP.
- **Managed Postgres / backups** — the compose Postgres is ephemeral dev data.
- **Managed MLflow / artifact lifecycle** — the compose MLflow server is a
  reference service. Production should run it behind TLS and back its database
  and artifacts with durable storage.
- **A real secret store** — point `RESEARCH_PLUGIN_THUNDER_ENV_FILE` or
  `RESEARCH_PLUGIN_MODAL_ENV_FILE` at a mounted secret, or inject
  `THUNDER_COMPUTE_API_KEY`/`MODAL_*`/`LAMBDA_*`/`HF_TOKEN` from your platform's
  secret manager. Never bake them into the image.
- **The cleanup scheduler** — wire a managed cron to `POST /api/admin/cleanup`.
- **Human login OAuth** — the control plane is currently private/operator-run;
  device-flow OAuth is still backlog (open decision C).
- **The React UI** — served separately (cloud SPA + CORS per origin); this image
  is the API only. The viewer's degraded states (result content / figures
  unavailable in this mode) are served by the API; the SPA renders them.
- **S3 bucket lifecycle rules / alerting** — configure object expiry and
  reaper-lag / provision-failure alerts on your platform.
