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
| `docker-compose.yml` | Full local stack: control + Postgres (record store) + MinIO (S3-shape blob store), with a one-shot bucket creator. |
| `.env.example` | Documents the control-mode environment (§3.4 config matrix). Copy, fill, and keep out of version control. |
| `.dockerignore` | Keeps secrets, local state, the React UI, and tests out of the build context. |

## Quick start (local full stack)

```sh
# From the plugin root (research_plugin/):
docker compose -f deploy/docker-compose.yml up --build
```

This brings up:
- **Postgres** on `localhost:5432` — the record store (Postgres dialect).
- **MinIO** on `localhost:9000` (console `:9001`) — the S3-shape blob store; the
  `createbucket` job makes the `research-plugin-blobs` bucket.
- **control** on `localhost:8787` — auth ON, daemon task/sync-target endpoints
  ON, reaper ON.

Verify the control plane is up and learn the version floor:

```sh
curl -s http://localhost:8787/api/meta
# {"server_version":"...","min_daemon_version":"...","min_proxy_version":"..."}
```

Mint a tenant token (control container holds the AuthService); hand the token to
a daemon/proxy out of band and point it at `http://localhost:8787` via
`RESEARCH_PLUGIN_CONTROL_URL`.

## Modes & environment (§3.4)

The same image runs every mode; the entrypoint forces `control`. Key
control-mode variables (full list in `.env.example`):

| Variable | Required | Meaning |
|---|---|---|
| `RESEARCH_PLUGIN_MODE` | yes (forced) | `control` |
| `RESEARCH_PLUGIN_DB_URL` | prod | `postgres://…` (else SQLite — dev only) |
| `RESEARCH_PLUGIN_BLOB_BUCKET` + `AWS_*` | prod | object store; presign must be a reachable HTTPS PUT |
| `MODAL_*` / `LAMBDA_API_KEY` / `HF_TOKEN` | to provision | provider creds — **secret store only** in control mode (`.env` discovery is disabled); `HF_TOKEN` is delivered post-boot over the management channel, never embedded in VM `user_data` |

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
are unauthenticated for liveness/handshake; everything else requires a bearer
token.

## What this stack does NOT do (documented seams)

These are intentionally out of the reference stack — production owns them:

- **TLS certificates** — terminate at your LB; this image speaks HTTP.
- **Managed Postgres / backups** — the compose Postgres is ephemeral dev data.
- **A real secret store** — point `RESEARCH_PLUGIN_MODAL_ENV_FILE` at a mounted
  secret, or inject `MODAL_*`/`LAMBDA_*`/`HF_TOKEN` from your platform's secret
  manager. Never bake them into the image.
- **The cleanup scheduler** — wire a managed cron to `POST /api/admin/cleanup`.
- **Auth bootstrap beyond static per-tenant tokens** — device-flow OAuth is
  backlog (open decision C).
- **The React UI** — served separately (cloud SPA + CORS per origin); this image
  is the API only. The viewer's degraded states (result content / figures
  unavailable in this mode) are served by the API; the SPA renders them.
- **S3 bucket lifecycle rules / alerting** — configure object expiry and
  reaper-lag / provision-failure alerts on your platform.
