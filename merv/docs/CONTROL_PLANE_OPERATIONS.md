# Operating the Hosted Brain

This runbook covers the `control` deployment preset served by
`merv-control`. It is the operational companion to
`CONTROL_DATA_PLANE_SPLIT.md`; the reference container stack is documented in
`../deploy/README.md`.

## Security boundary

The current hosted brain is a private operator service, not a public SaaS API.
End-user authentication is not implemented: every HTTP request runs as the
implicit `local` principal, bearer tokens are not validated, and HTTP project
access is not tenant-isolated.

Consequences:

- place the brain behind TLS and a trusted network boundary;
- do not expose `/api/*`, `/mcp/*`, `/api/data-plane/*`, or `/api/admin/*`
  directly to the public internet;
- treat CORS and `X-RP-Client-Version` as browser/compatibility controls, not
  authentication;
- protect the separately served Merv UI and MLflow endpoint at the
  same infrastructure boundary.

## Topology and modes

Both presets use the same component graph: a brain owns records and policy,
while an agent-launched stdio MCP proxy performs checkout-local work.

| `MERV_MODE` | Brain preset | Record/blob defaults | Entrypoint |
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
entrypoint forces `MERV_MODE=control`.

## Required control configuration

The production control entrypoint has no checkout or staging directory, so it
fails fast unless these durable dependencies are configured:

- `MERV_DB_URL` — a `postgres://` or `postgresql://` record-store
  URL;
- `MERV_BLOB_BUCKET` plus the applicable `AWS_*` credential, region,
  and endpoint configuration — the S3-compatible submitted-byte store;
- `MERV_MGMT_KEY_PATH` — the mounted **private-key file**, readable
  by the control user and mode `0600` or stricter.

The management public key comes from `MERV_MGMT_PUBLIC_KEY` or an
adjacent `<private-key-path>.pub` file. The key is fingerprinted at startup;
changing it in place is rejected. Drain live sandboxes and restart the brain to
rotate it.

SQLite and local-directory blob fallbacks are available only to explicit
programmatic dev/test compositions that supply a `repo_root`; they are not a
fallback for the production console entrypoint.

### Browser CORS

`MERV_ALLOWED_ORIGINS` is a comma-separated list of exact HTTP(S)
origins allowed to call the brain from a browser. Control mode restricts CORS by
default; an empty list blocks cross-origin browser clients. Include the hosted
UI origin explicitly.

`MERV_CONTROL_RESTRICT_CORS=0` disables that restriction, but does
not add authentication and is inappropriate for an exposed deployment.

### Heavy-object storage

Heavy storage is optional and separate from the submitted-byte blob store:

```text
MERV_STORAGE_PROVIDER=s3
MERV_STORAGE_BUCKET=...
MERV_STORAGE_ENDPOINT_URL=...   # MinIO/R2/custom S3 endpoint
MERV_STORAGE_REGION=...
MERV_STORAGE_ACCESS_KEY_ID=...  # falls back to AWS_ACCESS_KEY_ID
MERV_STORAGE_SECRET_ACCESS_KEY=...
```

Presigned upload/download URLs must be reachable from the client-side proxies
that perform the transfers, not merely from inside the control container.

## Sandbox provider configuration

The default backend is Lambda Labs. Provider credentials belong in the control
process environment or an explicitly mounted provider env file; control mode
does not discover credentials from a user's checkout.

| Backend | Selection | Credentials |
|---|---|---|
| Lambda Labs | unset or `lambda_labs` | `MERV_LAMBDA_API_KEY`, `LAMBDA_LABS_API_KEY`, or `LAMBDA_API_KEY` |
| Thunder Compute | `thunder_compute` | `MERV_THUNDER_API_KEY`, `THUNDER_COMPUTE_API_KEY`, or `TNR_API_TOKEN` |
| Modal | `modal` | `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` |

Set `MERV_REQUIRE_SANDBOX_BACKEND=1` in production to make startup
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

- `MERV_MLFLOW_TRACKING_URI` is the public run-reachable endpoint
  returned to agents. Local agent processes and remote sandboxes must both be
  able to reach it.
- `MERV_MLFLOW_SERVER_URI` optionally gives the brain a different
  internal URL for metrics reads.
- `MERV_MLFLOW_DASHBOARD_URL` optionally gives humans a different
  browser URL.
- `MERV_REQUIRE_AGENT_MLFLOW=1` makes startup fail when agents would
  receive no tracking URI.

In the reference Compose stack, mounting MLflow under a path prefix requires
matching ingress routes and `MERV_MLFLOW_STATIC_PREFIX`. The routing
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
With the current unauthenticated HTTP surface, externally created projects use
the implicit `local` tenant.

## Periodic cleanup

The brain constructs `CleanupService` but does not run a cleanup scheduler.
Call the private operator endpoint from managed cron or a sidecar:

```http
POST /api/admin/cleanup
```

One pass performs four idempotent, best-effort sweeps:

1. **running-row reconciliation** — asks the provider whether each tracked
   running VM still exists and marks dead rows terminated;
2. **submitted-blob expiry** — deletes blobs past their TTL;
3. **heavy-storage expiry** — expires eligible object-ledger rows using
   refcount-aware cleanup;
4. **stale-provision cleanup** — fails or terminates provisioning rows that did
   not reach a usable VM before the deadline.

The first sweep does not enumerate and terminate provider VMs that have no
registry row. Provider-specific deterministic-name cleanup during provisioning
and stale-provision handling cover the implemented orphan defenses.

The in-process expiry reaper is separate from these broader periodic sweeps and
continues to run between cleanup calls.

## Observability

In control mode the brain writes one compact JSON record to stdout per HTTP
request. It includes request id, tenant id, path, method, status, and duration;
the reference image sets `PYTHONUNBUFFERED=1`.

```http
GET /api/admin/tenants/{tenant_id}/counters
```

The private counter endpoint reports `tool_calls` (currently counted from the
project event table), sandbox generations, and closed-generation sandbox hours
for the requested stored tenant id. Under the current unauthenticated surface,
request logs normally carry tenant `local`.

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

Production operators must additionally provide TLS termination, real user
authentication and authorization, managed Postgres and backups, a secret
store, the cleanup schedule, durable object lifecycle policy,
monitoring/alerting, and separately hosted UI/MLflow services.
