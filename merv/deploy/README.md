# Hosted brain reference deployment

This directory is a worked deployment of the Merv brain. The hosted
entry point uses the same `ControlApp` composition as the local brain, with
durable hosted adapters and stricter startup requirements. It is not a managed
service or a production security boundary.

The reference stack contains:

| Service | Responsibility |
|---|---|
| `control` | FastAPI brain: research records, workflow gates, reviews, sandbox lifecycle, UI API, and private proxy submission routes |
| `postgres` | Research records and a separate MLflow database |
| `minio` | Submitted-byte blobs, optional heavy-file storage, and MLflow artifacts |
| `mlflow` | Central tracking server |
| `mgmtkey` | Generates a development-only brain management SSH key |

The MCP proxy still runs on each agent machine. It reads the checkout, validates
and hashes files, uses the caller-provided SSH key path for explicit local
transfers without minting or persisting that key, and sends
only explicit metadata or bounded submitted bytes to the brain. The browser UI
is deployed separately and talks directly to the brain.

## Start the reference stack

From `merv/`:

```sh
export SUPABASE_URL=https://your-project.supabase.co
export SUPABASE_JWT_SECRET=your-rotated-jwt-secret
docker compose -f deploy/docker-compose.yml up --build
curl -s http://127.0.0.1:8787/api/meta
```

Compose refuses to start the hosted control service without those verifier
values. Use a secret-backed env file instead of shell exports outside local
development.

The compose defaults start the complete set of services, but intentionally make
the control container **record-only**:

- `RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` is empty, so agents are not given a
  run-reachable tracking URL.
- sandbox provider credentials are empty, so provisioning is unavailable.

Configure both before treating the stack as run-ready. Remote sandboxes must be
able to reach the MLflow tracking URL. Heavy-storage presigned URLs are used by
client-side MCP proxies (and the doctor), so they must be reachable from those
clients; they do not need to be reachable from sandbox execution.

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

`merv-control` forces `RESEARCH_PLUGIN_MODE=control`. With no
explicit development `repo_root`, startup requires:

- `RESEARCH_PLUGIN_DB_URL`: Postgres record store;
- `RESEARCH_PLUGIN_BLOB_BUCKET` plus the relevant `AWS_*` settings: durable
  submitted-byte blob store;
- `RESEARCH_PLUGIN_MGMT_KEY_PATH`: a mounted **private-key file** readable only
  by the control process;
- either `RESEARCH_PLUGIN_MGMT_PUBLIC_KEY` or an adjacent `<key>.pub` file;
- and `SUPABASE_URL` plus `SUPABASE_JWT_SECRET`: the mandatory hosted verifier.

Lambda Labs and Thunder Compute additionally require a trusted OpenSSH
known-hosts file at `RESEARCH_PLUGIN_MGMT_KNOWN_HOSTS_FILE`. The reference
Compose stack only passes that path; it does not mount, populate, or dynamically
enroll host keys, and startup does not validate the file. Keep VM provisioning
disabled until keys are obtained through a trusted provider channel and the
file is persistently mounted inside the control container. An empty file,
`ssh-keyscan` over the untrusted path, or `accept-new` is not a secure substitute.
Modal uses its authenticated provider control channel and is unaffected by this
specific requirement.

Heavy object storage is optional. Enable it with
`RESEARCH_PLUGIN_STORAGE_PROVIDER` and the storage bucket/credentials. This is
separate from the submitted-byte blob store, which hosted startup requires.

Central MLflow has three URLs because callers, the brain, and people may reach
it differently:

| Variable | Consumer |
|---|---|
| `RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` | agents and sandbox commands; must be reachable from every run location |
| `RESEARCH_PLUGIN_MLFLOW_SERVER_URI` | brain metrics reads; may use an internal service URL |
| `RESEARCH_PLUGIN_MLFLOW_DASHBOARD_URL` | links opened by people; defaults to the tracking URL |

Set `RESEARCH_PLUGIN_REQUIRE_AGENT_MLFLOW=1` to reject startup without an agent
tracking URL. Set `RESEARCH_PLUGIN_REQUIRE_SANDBOX_BACKEND=1` to reject startup
when the selected provider is unhealthy. Provider credentials and the brain
management key belong only in the hosted secret store; they are never shipped
to the MCP proxy.

See `.env.example` for the supported variables.

## Network and security boundary

The brain serves plain HTTP on port 8787. A real deployment must terminate TLS
at a load balancer or reverse proxy. In the reference Compose stack, if MLflow
is exposed under `/mlflow`, set
`RESEARCH_PLUGIN_MLFLOW_STATIC_PREFIX=/mlflow`; Compose forwards it to MLflow's
`--static-prefix`. Route MLflow's tracking, artifact, UI, and `ajax-api` paths
consistently. The Python brain itself does not read this variable.

Hosted control refuses startup without Supabase authentication, and project
membership enforces tenant-facing resource access. CORS restrictions and the
MCP client-version floor remain independent controls. Keep the brain, MLflow,
storage endpoints, and admin routes on a trusted operator network; do not
expose the reference compose stack directly to the public internet.

The UI may call control/lifecycle routes, but checkout-local data-plane
mutations remain private proxy routes. The brain never receives a checkout root
and cannot serve arbitrary live checkout files.

## Operations

- Clients send `X-RP-Client-Version`; clients below the floor published by
  `/api/meta` receive HTTP 426.
- The sandbox expiry reaper runs inside hosted control. On restart, it
  reconciles registered active rows.
- Broader cleanup is not scheduled. Invoke `POST /api/admin/cleanup` from a
  trusted cron or sidecar. It handles registered running-row reconciliation,
  blob TTLs, and storage leases; stale provisioning is also checked by the
  in-process sandbox reaper and repeated by this endpoint for defense in depth.
  Neither path discovers arbitrary provider VMs that have no ledger row.
- HTTP request logs go to stdout. Diagnostic activity and tool-call rings are
  process-local and bounded, so they reset on restart.

## What production must add

- TLS, routing, and a trusted network boundary;
- managed Postgres, object storage, backups, and lifecycle rules;
- a real secret manager and management-key rotation procedure;
- trusted VM host-key enrollment and a persistent known-hosts mount before
  enabling Lambda Labs or Thunder Compute;
- a cleanup scheduler and operational alerting;
- a separately deployed UI with explicit CORS origins; and
- Supabase secret rotation, existing-project membership backfill, and separate
  operator/admin authorization before any public or multi-tenant use.
