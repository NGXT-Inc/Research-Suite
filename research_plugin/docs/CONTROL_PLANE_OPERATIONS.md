# Operating the control plane

**Status:** current (cloud backend migration Phase 9) · the operational
companion to `docs/CLOUD_BACKEND_MIGRATION_PLAN.md`.

This is the run-the-cloud guide: modes, environment, the cleanup jobs, the
version floor, cost governance, observability, and the reference deploy. Local
mode is unaffected by everything here — it is the byte-identical default.

## Modes

`RESEARCH_PLUGIN_MODE` selects the process role (`backend/config.py`):

| Mode | Role | Auth | Reaper | Auto-rsync | Console script |
|---|---|---|---|---|---|
| `local` (default) | both planes in one process | off | on | on | `research-plugin-http` |
| `control` | cloud control plane | **on** | on | off | `research-plugin-control` |
| `daemon` | user-machine data plane | off (auths upstream) | off | on | `research-plugin-daemon` |

Mode validation is fail-fast: a daemon without `RESEARCH_PLUGIN_CONTROL_URL`, or
an unknown mode value, refuses to start.

## Environment (control mode, §3.4)

See `deploy/.env.example` for a copy-ready template. Essentials:

- `RESEARCH_PLUGIN_DB_URL` — `postgres://…` selects the Postgres dialect.
  **Required in production** (SQLite fallback is dev-only).
- `RESEARCH_PLUGIN_BLOB_BUCKET` + `AWS_*` — the S3-shape blob store. The presign
  must be a real HTTPS PUT a sandbox VM can reach (the parachute depends on it).
- Provider creds (`MODAL_*`, `LAMBDA_API_KEY`, `HF_TOKEN`) — **secret store /
  process env ONLY** in control mode. User-machine `.env` discovery is disabled;
  an explicit `RESEARCH_PLUGIN_MODAL_ENV_FILE` (a mounted secret) is the seam.
- `HF_TOKEN` is delivered to a fresh VM **post-boot over the management channel**
  (`SandboxBackend.write_secrets` → `/opt/rp/secrets.env`), never embedded in VM
  `user_data` (risk 16).

## Version / compatibility floor

- `GET /api/meta` →
  `{server_version, min_daemon_version, min_proxy_version, mode, capabilities}`.
  Unauthenticated, so a client can discover the floor before holding a token and
  hide local data-plane actions when connected to hosted control.
- Clients (daemon + stdio proxy) stamp `X-RP-Client-Version` on every request.
- A **below-floor** client gets `426 Upgrade Required` with an actionable
  message (`error_code: client_too_old`) **before** auth. A missing header is
  tolerated (pre-Phase-9 clients). Floors are constants in `backend/version.py`;
  the contract is additive-only within a major, so the floor moves rarely.

## Cost governance

- **Quotas** (`QuotaService`): per-tenant ceilings on concurrent sandboxes,
  per-request time limit, instance price, and running-total **GPU-hour / USD
  budgets** reconstructed from the `sandbox_generations` ledger (an open
  generation bills to now). A tenant with no quota row is unlimited (local mode).
- **Spend kill-switch**: a per-tenant or **global** circuit breaker that refuses
  new provisioning when tripped. Trip/arm via `QuotaService.set_kill_switch`
  (runbook action). Checked first in `check_admission`.
- The reaper's env off-switch is **ignored in control mode** — the cloud holds
  the keys and pays for every VM, so it can never be told to stop reaping.

## Cleanup jobs (scheduling is a seam)

The control plane BUILDS the idempotent cleanup sweeps but does **not** run a
scheduler. `CleanupService.run_all(now=…)` (in `backend/services/cleanup.py`)
runs four sweeps:

1. **orphan-VM sweep** — reconcile running rows against the provider; terminate
   rows whose VM is gone.
2. **blob TTL GC** — delete expired blobs across tenants.
3. **lease-expiry sweep** — release abandoned sync leases.
4. **stale `awaiting_initial_push` reap** — terminate billing VMs whose initial
   push never completed past a deadline (a dead daemon mid-provision, risk 8).

Wire a managed cron / sidecar tick to **`POST /api/admin/cleanup`** on your
cadence. The reaper thread (billing protection) IS owned and runs on its own
interval; the sweeps are the broader periodic housekeeping.

## Observability

- One **structured JSON log line per HTTP request** to stdout in control mode:
  `request_id` + `tenant_id` + `path` + `status` + `duration_ms`, redacted via
  the shared `SENSITIVE_KEYS` (no token/capability ever reaches stdout). Run with
  `PYTHONUNBUFFERED=1` (the deploy image sets it).
- The response carries `X-RP-Request-Id` for correlation.
- **Per-tenant counters**: `GET /api/admin/tenants/{tenant_id}/counters` (tool
  calls, sandbox generations, sandbox-hours). The audit trail reuses the
  append-only `events` table scoped by project → tenant (open decision J:
  cloud-only, no thin local mirror; the daemon keeps its own `activity.jsonl`,
  never synced).

## Degraded UI states (server side)

Result-role `/content` and figure `/file?rel=` return a documented
`content_unavailable` shape in control mode (the bytes live only on an offline
daemon, or are metadata-only — fixed decision 6) rather than a 500. `sandbox.sync`
surfaces a `daemon_unreachable` reason on a task timeout. Hosted browser/MCP
`sandbox.release` is a lifecycle action only: it terminates without local
final-pull rsync and returns `final_pull_skipped`. Reaper/local release paths may
still attempt a best-effort final pull; failures flag `daemon_unreachable` while
still freeing billing. The React SPA renders these states — that repoint is
separate from the backend.

## Poll amplification

The UI is 3 s polling; `sandbox.terminal` is a management-key SSH read. A
control-side **transcript cursor cache** (`backend/services/transcript_cache.py`,
bounded + TTL'd) coalesces repeated reads per sandbox; the lint cache (Phase 2)
covers gate reads. SSE/push is backlog.

## Reference deploy

`deploy/` is the reference stack (Dockerfile + docker-compose with Postgres +
MinIO + `.env.example`). It is NOT a managed deploy — TLS termination, managed
Postgres, a real secret store, the cleanup scheduler, S3 lifecycle rules, and
alerting are the operator's responsibility. See `deploy/README.md`.

## Known seams (not built — production owns them)

Managed Postgres provisioning, real TLS certs, a live cleanup scheduler daemon,
device-flow OAuth (open decision C; static per-tenant tokens are v1), the React
UI repoint, and reaper-lag / provision-failure / parachute-failure alerting.
