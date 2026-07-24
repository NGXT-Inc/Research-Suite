# Hosted brain reference deployment

This directory is a worked deployment of the Merv brain. The hosted
entry point uses the same `ControlApp` composition as the local brain, with
durable hosted adapters and stricter startup requirements. It is not a managed
service or a production security boundary.

The reference stack contains:

| Service | Responsibility |
|---|---|
| `control` | FastAPI brain: research records, workflow gates, reviews, sandbox lifecycle, UI API, and token-authorized upload routes |
| `postgres` | Research records and a separate MLflow database |
| `minio` | Submitted-byte blobs, optional heavy-file storage, and MLflow artifacts |
| `mlflow` | Central tracking server |
| `mgmtkey` | Generates a development-only brain management SSH key |

Each agent (local Claude Code, cloud Codex, Replit) connects directly to the
brain's `POST /mcp` endpoint with an `Authorization: Bearer <key>` project key.
There is no local MCP proxy on agent machines, and agents never send a checkout
root; they send only explicit metadata or bounded submitted bytes to the brain.
The browser UI is deployed separately and talks directly to the brain.

## Start the reference stack

From `merv/`:

```sh
docker compose -f deploy/docker-compose.yml up --build
curl -s http://127.0.0.1:8787/api/meta
```

Provider credentials use a separate container env file so Compose cannot erase
them with empty `environment` defaults. Keep that file outside the checkout,
restrict it to the deployment account, and pass its absolute path when starting
the stack:

```sh
MERV_PROVIDER_ENV_FILE=/run/secrets/merv-provider.env \
  docker compose -f deploy/docker-compose.yml up --build -d
```

The file may contain `MERV_LAMBDA_API_KEY` (or
`LAMBDA_LABS_API_KEY`), Thunder/Modal credentials, and `HF_TOKEN`. Do not also
declare those names with empty values under the control service's
`environment:` map: Compose gives that map precedence over `env_file`.

The compose defaults start the complete set of services, but intentionally make
the control container **record-only**:

- `MERV_MLFLOW_TRACKING_URI` is empty, so agents are not given a
  run-reachable tracking URL.
- sandbox provider credentials are empty, so provisioning is unavailable.

Configure both before treating the stack as run-ready. Remote sandboxes must be
able to reach the MLflow tracking URL. Heavy-storage presigned URLs are run by
agent clients (and the doctor), so they must be reachable from those machines;
they do not need to be reachable from sandbox execution.

Run the active readiness sweep after a deploy or restart:

```sh
python3 deploy/doctor.py --control-url http://127.0.0.1:8787
```

The doctor creates or reuses a smoke project, writes an MLflow run, checks the
sandbox provider, and exercises heavy object storage. It therefore fails on the
record-only defaults. `--skip-mlflow-write` skips only the write smoke (not
MLflow configuration/health); `--skip-storage` skips the storage smoke.

For the local MinIO stack, a host-run doctor may need its Docker hostname
rewritten to the published port:

```sh
RP_DOCTOR_URL_REWRITE=http://minio:9000=http://127.0.0.1:9000 \
  python3 deploy/doctor.py --control-url http://127.0.0.1:8787
```

## Hosted configuration

`merv-control` forces `MERV_MODE=control`. With no
explicit development `repo_root`, startup requires:

- `MERV_DB_URL`: Postgres record store;
- `MERV_BLOB_BUCKET` plus the relevant `AWS_*` settings: durable
  submitted-byte blob store;
- `MERV_MGMT_KEY_PATH`: a mounted **private-key file** readable only
  by the control process; and
- either `MERV_MGMT_PUBLIC_KEY` or an adjacent `<key>.pub` file.

Heavy object storage is optional. Enable it with
`MERV_STORAGE_PROVIDER` and the storage bucket/credentials. This is
separate from the submitted-byte blob store, which hosted startup requires.

Central MLflow has three URLs because callers, the brain, and people may reach
it differently:

| Variable | Consumer |
|---|---|
| `MERV_MLFLOW_TRACKING_URI` | agents and sandbox commands; must be reachable from every run location |
| `MERV_MLFLOW_SERVER_URI` | brain metrics reads; may use an internal service URL |
| `MERV_MLFLOW_DASHBOARD_URL` | links opened by people; defaults to the tracking URL |

Set `MERV_REQUIRE_AGENT_MLFLOW=1` to reject startup without an agent
tracking URL. Set `MERV_REQUIRE_SANDBOX_BACKEND=1` to reject startup
when the selected provider is unhealthy. Provider credentials and the brain
management key belong only in the hosted secret store; they are never sent to
agent clients.

See `.env.example` for the supported variables.

### Legacy `RESEARCH_PLUGIN_*` names

`MERV_*` is the primary spelling for every variable; the legacy
`RESEARCH_PLUGIN_*` names keep working forever as a fallback (non-empty
`MERV_*` wins, and a legacy-sourced value logs one deprecation line). The
reference compose file also dual-reads host-side substitutions, so a host
that still exports only legacy names deploys unchanged.

One sharp edge for operators with their own compose **override files**:
`environment:` maps merge by key. This base file now sets container env
under the `MERV_*` keys, and a non-empty `MERV_*` beats a legacy name inside
the container — so an override that pins values under `RESEARCH_PLUGIN_*`
keys no longer shadows the base defaults. Rename the keys in your override
to `MERV_*` (or export the value host-side, which the base dual-reads).

## Network and security boundary

The brain serves plain HTTP on port 8787. A real deployment must terminate TLS
at a load balancer or reverse proxy. In the reference Compose stack, if MLflow
is exposed under `/mlflow`, set
`MERV_MLFLOW_STATIC_PREFIX=/mlflow`; Compose forwards it to MLflow's
`--static-prefix`. Route MLflow's tracking, artifact, UI, and `ajax-api` paths
consistently. The Python brain itself does not read this variable.

The reference compose stack ships with authentication off by default. Hosted
control can enforce end-user authentication (set `MERV_REQUIRE_AUTH=1` with
Supabase configuration), with `project_members` tenant isolation and
project-scoped `mk_` keys (the gateway enforces that a key can only act on its
bound project), but the reference stack leaves it disabled. CORS restrictions
and the MCP client-version floor are not authentication. Keep the auth-off
reference stack — the brain, MLflow, storage endpoints, and admin routes — on a
trusted operator network; do not expose it directly to the public internet.

The UI may call control/lifecycle routes, but byte transfers — artifact,
storage, and feed uploads, and sandbox output pulls — run agent-side over
presigned or token URLs. The brain never receives a checkout root and cannot
serve arbitrary live checkout files.

## Operations

- Clients send `X-RP-Client-Version`; clients below the floor published by
  `/api/meta` receive HTTP 426.
- The sandbox expiry reaper runs inside hosted control. On restart, it
  reconciles registered active rows.
- Broader cleanup is not scheduled. Invoke `POST /api/admin/cleanup` from a
  trusted cron or sidecar. It handles registered stale sandbox state, blob TTLs,
  storage leases, and stale provisioning records; it does not discover and
  terminate arbitrary provider VMs that have no ledger row.
- HTTP request logs go to stdout. Diagnostic activity and tool-call rings are
  process-local and bounded, so they reset on restart.

## What production must add

- TLS, routing, and a trusted network boundary;
- managed Postgres, object storage, backups, and lifecycle rules;
- a real secret manager and management-key rotation procedure;
- a cleanup scheduler and operational alerting;
- a separately deployed UI with explicit CORS origins; and
- end-user authentication and authorization before any public or multi-tenant
  use.
