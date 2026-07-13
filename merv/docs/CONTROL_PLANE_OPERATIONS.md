# Operating the Hosted Brain

This runbook covers the `control` deployment preset served by
`merv-control`. It is the operational companion to
`CONTROL_DATA_PLANE_SPLIT.md`; the reference container stack is documented in
`../deploy/README.md`.

## Security boundary

The hosted brain refuses to start without a Supabase verifier. Except for its
documented bootstrap/liveness routes, HTTP requests require a verified bearer
credential, and project routes require project membership. Operator endpoints
do not have a separate administrator role, so the brain remains a private
operator service rather than a public SaaS API.

Consequences:

- place the brain behind TLS and a trusted network boundary;
- do not expose `/api/*`, `/mcp/*`, `/api/data-plane/*`, or `/api/admin/*`
  directly to the public internet;
- treat CORS and `X-RP-Client-Version` as browser/compatibility controls; bearer
  verification remains the authentication boundary;
- protect the separately served Merv UI and MLflow endpoint at the
  same infrastructure boundary.

## Topology and modes

Both presets use the same component graph: a brain owns records and policy,
while an agent-launched stdio MCP proxy performs checkout-local work.

| `RESEARCH_PLUGIN_MODE` | Brain preset | Record/blob defaults | Entrypoint |
|---|---|---|---|
| `local` (default) | loopback development brain | SQLite and local-directory blobs | `merv-http` |
| `control` | hosted private brain | Postgres, S3-compatible blobs, mounted management key | `merv-control` |

The proxy is the data plane in both modes. It reads and validates repo files,
uses a caller-provided SSH key path for explicit output pulls without minting
or persisting that key, and stores local checkout-to-project links. The brain
never receives `repo_root` and never dials a user machine.

Both brains own sandbox provider lifecycle and the expiry reaper. Neither
preset automatically copies files out of a sandbox.

Unknown mode values fail at startup. The `merv-control` console
entrypoint forces `RESEARCH_PLUGIN_MODE=control`.

## Required control configuration

The production control entrypoint has no checkout or staging directory, so it
fails fast unless these durable dependencies are configured:

- `RESEARCH_PLUGIN_DB_URL` — a `postgres://` or `postgresql://` record-store
  URL;
- `RESEARCH_PLUGIN_BLOB_BUCKET` plus the applicable `AWS_*` credential, region,
  and endpoint configuration — the S3-compatible submitted-byte store;
- `RESEARCH_PLUGIN_MGMT_KEY_PATH` — the mounted **private-key file**, readable
  by the control user and mode `0600` or stricter; and
- `SUPABASE_URL` and `SUPABASE_JWT_SECRET` — the mandatory hosted request
  verifier. Startup fails when either is missing.

VM-backed management operations separately require
`RESEARCH_PLUGIN_MGMT_KNOWN_HOSTS_FILE`, which defaults to
`~/.ssh/known_hosts` inside the control container. Configuration does not mount
or populate it and startup does not validate it. Populate entries through a
trusted provider channel before contacting Lambda Labs or Thunder Compute;
nonstandard ports use OpenSSH's `[host]:port` form. Unknown or changed keys fail
closed. The reference stack does not yet implement secure dynamic enrollment.

The management public key comes from `RESEARCH_PLUGIN_MGMT_PUBLIC_KEY` or an
adjacent `<private-key-path>.pub` file. The key is fingerprinted at startup;
changing it in place is rejected. Drain live sandboxes and restart the brain to
rotate it.

SQLite and local-directory blob fallbacks are available only to explicit
programmatic dev/test compositions that supply a `repo_root`; they are not a
fallback for the production console entrypoint.

### Browser CORS

`RESEARCH_PLUGIN_ALLOWED_ORIGINS` is a comma-separated list of exact HTTP(S)
origins allowed to call the brain from a browser. Control mode restricts CORS by
default; an empty list blocks cross-origin browser clients. Include the hosted
UI origin explicitly.

`RESEARCH_PLUGIN_CONTROL_RESTRICT_CORS=0` disables that restriction, but does
not relax mandatory authentication and is inappropriate for an exposed deployment.

### Heavy-object storage

Heavy storage is optional and separate from the submitted-byte blob store:

```text
RESEARCH_PLUGIN_STORAGE_PROVIDER=s3
RESEARCH_PLUGIN_STORAGE_BUCKET=...
RESEARCH_PLUGIN_STORAGE_ENDPOINT_URL=...   # MinIO/R2/custom S3 endpoint
RESEARCH_PLUGIN_STORAGE_REGION=...
RESEARCH_PLUGIN_STORAGE_ACCESS_KEY_ID=...  # falls back to AWS_ACCESS_KEY_ID
RESEARCH_PLUGIN_STORAGE_SECRET_ACCESS_KEY=...
```

Presigned upload/download URLs must be reachable from the client-side proxies
that perform the transfers, not merely from inside the control container.

## Sandbox provider configuration

The default backend is Lambda Labs. Provider credentials belong in the control
process environment or an explicitly mounted provider env file; control mode
does not discover credentials from a user's checkout.

| Backend | Selection | Credentials |
|---|---|---|
| Lambda Labs | unset or `lambda_labs` | `RESEARCH_PLUGIN_LAMBDA_API_KEY`, `LAMBDA_LABS_API_KEY`, or `LAMBDA_API_KEY` |
| Thunder Compute | `thunder_compute` | `RESEARCH_PLUGIN_THUNDER_API_KEY`, `THUNDER_COMPUTE_API_KEY`, or `TNR_API_TOKEN` |
| Modal | `modal` | `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` |

Set `RESEARCH_PLUGIN_REQUIRE_SANDBOX_BACKEND=1` in production to make startup
fail when the selected provider's health check fails. Without it, the brain can
start as a record-only service and `sandbox.health` reports the provider error.

Provider secret delivery differs by backend. Lambda Labs and Thunder Compute
deliver `HF_TOKEN` after boot over the management channel into
`/opt/rp/secrets.env`. Modal supplies it through `modal.Secret` at sandbox
creation and writes the runtime environment under `/opt/rp/env`. Secret values
are never returned through the agent API or written into retained artifacts by
the plugin.

## Centralized MLflow

MLflow is the quantitative ledger; the brain does not keep a second metrics
database.

- `RESEARCH_PLUGIN_MLFLOW_TRACKING_URI` is the public run-reachable endpoint
  returned to agents. Local agent processes and remote sandboxes must both be
  able to reach it.
- `RESEARCH_PLUGIN_MLFLOW_SERVER_URI` optionally gives the brain a different
  internal URL for metrics reads.
- `RESEARCH_PLUGIN_MLFLOW_DASHBOARD_URL` optionally gives humans a different
  browser URL.
- `RESEARCH_PLUGIN_REQUIRE_AGENT_MLFLOW=1` makes startup fail when agents would
  receive no tracking URI.

In the reference Compose stack, mounting MLflow under a path prefix requires
matching ingress routes and `RESEARCH_PLUGIN_MLFLOW_STATIC_PREFIX`. The routing
example is in `../deploy/README.md`.

## Client compatibility

```http
GET /api/meta
```

The response publishes `server_version`, `min_proxy_version`, `mode`, and
browser capabilities. Proxies and the UI send `X-RP-Client-Version`. In control
mode, a version below the floor receives `426 client_too_old`; a missing header
is currently tolerated.

The response header `X-RP-Request-Id` identifies each HTTP request for log
correlation.

## Sandbox reaping and cost controls

The expiry reaper runs inside the brain and is forced on in control mode. Its
environment off-switch is ignored because the control process holds provider
credentials and is responsible for billing cleanup.

`QuotaService` can enforce concurrent-sandbox, request-duration,
instance-price, GPU-hour, and USD limits from the sandbox-generation ledger. It
also supports global and per-tenant provisioning kill switches. There is no
public quota-management HTTP surface; these are operator/service integrations.
The current identity layer maps authenticated principals to the shared `local`
tenant; project membership remains the per-user authorization boundary.

## Periodic cleanup

The sandbox daemon already reaps expired rows and stale provisions on its
in-process cadence. The brain also constructs `CleanupService`, but does not
schedule the broader blob/storage/running-row sweeps. Call the private operator
endpoint from managed cron or a sidecar:

```http
POST /api/admin/cleanup
```

One pass performs four idempotent, best-effort sweeps:

1. **running-row reconciliation** — asks the provider whether each tracked
   running VM still exists and marks dead rows terminated;
2. **submitted-blob expiry** — deletes blobs past their TTL;
3. **heavy-storage expiry** — expires eligible object-ledger rows using
   refcount-aware cleanup;
4. **stale-provision cleanup** — repeats the in-process safety pass for
   provisioning rows that did not reach a usable VM before the deadline.

The first sweep does not enumerate and terminate provider VMs that have no
registry row. Provider-specific deterministic-name cleanup during provisioning
and stale-provision handling cover the implemented orphan defenses.

The in-process expiry/stale-provision reaper continues to run between cleanup
calls.

## Observability

In control mode the brain writes one compact JSON record to stdout per HTTP
request. It includes request id, tenant id, path, method, status, and duration;
the reference image sets `PYTHONUNBUFFERED=1`.

```http
GET /api/admin/tenants/{tenant_id}/counters
```

The private counter endpoint reports `tool_calls` (currently counted from the
project event table), sandbox generations, and closed-generation sandbox hours
for the requested stored tenant id. With the current identity mapping, request
logs normally carry tenant `local`.

Three different records serve different purposes:

- project `events` are durable and committed with accepted research changes;
- `/api/activity` is a bounded in-memory summarized activity ring;
- `/api/debug/tool-calls` is a bounded in-memory full tool-I/O ring.

The two diagnostic rings reset whenever the brain restarts. They are not JSONL
or SQLite audit files.

## UI traffic and degraded content

The Merv UI uses project events over SSE for prompt refreshes, with
ETag-based conditional polling as fallback. Desktop fallback polling is 3 s;
mobile uses 5 s while work is live and 30 s when quiet. Detail views may own
slower safety pollers for external state such as MLflow.

Terminal reads use management SSH and are coalesced by the bounded,
TTL-controlled transcript cache. Metrics sampling is also briefly coalesced.
Management operations require a matching trusted host key and fail closed;
this includes post-boot secret delivery.

Because the brain has no checkout access:

- submitted gated documents and captured relative figures can be rendered from
  the blob store;
- non-gated repo file bodies return an explicit unavailable response;
- direct `/file` reads cannot expose live checkout files.

Browser sandbox release is destructive. The UI confirms retention before
calling the release route, which terminates the VM directly; anything not
pulled or uploaded beforehand is lost.

## Reference deployment and readiness

`deploy/docker-compose.yml` starts a reference control brain with Postgres,
MinIO, centralized MLflow, and a dev-only mounted management key. It is a local
integration stack, not a managed production deployment.

After supplying a run-reachable MLflow tracking URI and healthy sandbox-provider
credentials, run:

```bash
python3 deploy/doctor.py --control-url http://127.0.0.1:8787
```

The doctor actively checks the control API, MLflow tracking/write path, sandbox
provider health/options, and object-storage upload/download. The default Compose
stack may intentionally start without provider credentials or an agent tracking
URI; in that record-only configuration the full doctor is expected to fail.

Production operators must additionally provide TLS termination, Supabase secret
rotation and membership backfill, separate operator/admin authorization,
managed Postgres and backups, a secret store, trusted VM host-key enrollment,
the cleanup schedule, durable object lifecycle policy, monitoring/alerting,
and separately hosted UI/MLflow services.
