# Browser HTTP API

The Research State UI talks directly to the brain's `/api/*` HTTP surface. The
same brain also serves `/mcp/*` to local stdio proxies, but the browser is not an
agent runtime and has no chat endpoint.

The route modules under `backend/transport/api/` and the projections in
`backend/transport/api/views.py` are the executable source of truth for this
document.

## Runtime and trust boundary

- Project scope is explicit in the URL. The UI selects a `project_id` and uses
  `/api/projects/{project_id}/...`; the brain does not infer a current project.
- The brain never receives a checkout root and never reads a user's checkout.
  Checkout-local work belongs to the stdio MCP proxy.
- The supported browser surface does not register or associate repo files,
  materialize folders, pull sandbox outputs, or upload/download local storage
  files. Those operations run through proxy-local MCP tools and submit
  validated facts or bytes to the brain.
- Local and hosted brains expose the same HTTP shape. Local mode normally uses
  SQLite and local blobs; control mode uses operator-configured durable stores.
- End-user authentication is not implemented. Every request currently runs as
  the implicit `local` principal, including in control mode. Authorization
  headers are accepted by CORS but are not an identity or tenant boundary. A
  hosted brain must remain on a trusted network.

## Server identity and compatibility

```http
GET /health
GET /api/meta
```

`/health` is a liveness response:

```json
{"ok": true, "version": "<server version>"}
```

`/api/meta` is the client handshake:

```json
{
  "server_version": "0.0011",
  "min_proxy_version": "0.0011",
  "mode": "local",
  "capabilities": {
    "hosted_control": false,
    "local_data_plane_http": false,
    "resource_registration": false,
    "resource_association": false
  }
}
```

Both deployment presets report `local_data_plane_http: false`: browser-local
file mutation is not part of the current architecture. In control mode, a
request carrying an `X-RP-Client-Version` below `min_proxy_version` receives
`426 client_too_old`. A missing version header is currently tolerated.

## Refresh, caching, and events

These snapshot endpoints support `ETag` and `If-None-Match`; an unchanged
snapshot returns `304`:

```http
GET /api/projects/{project_id}/home
GET /api/projects/{project_id}/sandboxes
GET /api/projects/{project_id}/events?limit=500
```

The UI uses server-sent events first and conditional polling as fallback:

```http
GET /api/projects/{project_id}/events/stream
```

The stream tails the durable project event table. It emits:

- `hello` with the initial cursor;
- `append` for each accepted event, with an SSE id;
- `state` after a non-empty batch, prompting one coalesced snapshot refresh;
- `ping` as a liveness heartbeat.

`?since=` overrides `Last-Event-ID`; otherwise a new connection starts at the
current head. `poll_ms` controls the server-side tail interval and `max_ms` can
bound a stream session. The production UI normally lets `EventSource` reconnect
using the server's retry hint.

## Projects and the home snapshot

```http
GET   /api/projects
POST  /api/projects
GET   /api/projects/{project_id}
PATCH /api/projects/{project_id}
PUT   /api/projects/{project_id}
GET   /api/projects/{project_id}/home
GET   /api/projects/{project_id}/status?experiment_id={experiment_id}
```

Create projects with `name` and `summary`. Do not send a repo path: checkout to
project links are machine-local proxy state.

`/home` is the primary UI bootstrap. It returns `project`, `claims`, the full
`experiments` list, `resources`, `reviews`, `recent_events`, `stats`, `workflow`,
`active_experiment`, `active_experiments`, `active_processes`, and MLflow health.
`active_experiments` contains non-terminal work with its workflow, sandboxes,
and active processes. `active_processes` includes both `provisioning` and
`running` sandboxes.

## Claims

```http
GET   /api/projects/{project_id}/claims
POST  /api/projects/{project_id}/claims
GET   /api/projects/{project_id}/claims/{claim_id}
GET   /api/projects/{project_id}/claims/{claim_id}/evidence
PATCH /api/projects/{project_id}/claims/{claim_id}
PUT   /api/projects/{project_id}/claims/{claim_id}
```

Claim creation accepts `statement`, `scope`, and `confidence`. Updates use the
same control-plane validation as MCP claim mutations.

`/evidence` is the claim ↔ quantitative-record join: the claim, MLflow health,
and one item per experiment testing the claim — its status, attempt index,
conclusion, compact review verdicts, and its bounded MLflow `metrics` payload
in the same shape as the project `/mlflow` overview items. It is a read-time
join over existing records, not a second quantitative ledger.

## Experiments and MLflow

```http
GET  /api/projects/{project_id}/experiments
GET  /api/projects/{project_id}/experiments?status={status}
GET  /api/projects/{project_id}/experiments/view
POST /api/projects/{project_id}/experiments
GET  /api/projects/{project_id}/experiments/{experiment_id}
GET  /api/projects/{project_id}/experiments/{experiment_id}/status
GET  /api/projects/{project_id}/experiments/{experiment_id}/figure
GET  /api/projects/{project_id}/experiments/{experiment_id}/graph
POST /api/projects/{project_id}/experiments/{experiment_id}/transition
GET  /api/projects/{project_id}/experiments/{experiment_id}/results/metrics
GET  /api/projects/{project_id}/mlflow
```

Create an experiment with `name`, `intent`, and `claim_ids`. Transitions accept
`{"transition": "...", "evidence": {...}}`; render the server-provided
`allowed_transitions` and `workflow.next_action` instead of maintaining a second
workflow table in the UI.

`/figure` is the system-derived experiment view. `/graph` is the submitted,
agent-authored logic graph plus lint problems and resolved references.

`.../results/metrics` is a bounded UI view over centralized MLflow: matching
runs, params, final values, and downsampled metric histories. It is not a second
metrics database. `/mlflow` aggregates that view across the project's
experiments and provides dashboard links when configured; each overview item
also carries `tested_claims` (compact claim identity + belief state) so ledger
surfaces can link runs back to the claims they are evidence for.

Both metrics payloads may carry `advisories` (with `advisory_note`): the
brain's deterministic "this metric looks off, and here is why" observations
over the attempt-window histories (see docs/CENTRALIZED_MLFLOW.md). They are
observations for the reader — the system takes no action on them.

## Reflections

```http
GET /api/projects/{project_id}/reflections
GET /api/projects/{project_id}/reflections/{reflection_id}
GET /api/projects/{project_id}/reflections/current/graph
GET /api/projects/{project_id}/reflections/{reflection_id}/graph
```

These are the canonical reflection-wave paths. Some response keys retain their
older synthesis names (`syntheses`, `open_synthesis`, `synthesis_id`), and the
stored review-stage status is `synthesis_review`.

The overview returns full wave states, the open/latest wave, and the project
reflection staleness signal. A wave includes its five-lens roster, corpus,
attempt-scoped resources, reviews, lens coverage, gate checklist, graph diff,
and allowed transitions. Per-wave graph reads use the version pinned by that
wave, so historical graphs remain faithful after the living graph changes.

## Resources

```http
GET    /api/projects/{project_id}/resources
GET    /api/projects/{project_id}/resources?kind={kind}
GET    /api/projects/{project_id}/resources/tree
GET    /api/projects/{project_id}/resources/{resource_id}
GET    /api/projects/{project_id}/resources/{resource_id}/history
GET    /api/projects/{project_id}/resources/{resource_id}/content
GET    /api/projects/{project_id}/resources/{resource_id}/content?version={version_id}
GET    /api/projects/{project_id}/resources/{resource_id}/file?rel={relative_path}
DELETE /api/projects/{project_id}/resources/{resource_id}
```

There are deliberately no browser `POST /resources` or `POST /associate`
routes. The proxy's `resource.register` tool observes the local file and uses
internal `/api/data-plane/resources/*` submissions. Associations of gated
documents capture the submitted document and referenced figure bytes in the
brain's blob store.

Content behavior follows the plane boundary:

- `/content?version=` serves an exact associated submitted version when its blob
  is available.
- `/content` serves the newest available submitted bytes for a gated document.
- Non-gated checkout files are metadata-only to the brain and return
  `available: false` with `reason: content_unavailable_in_this_mode`.
- `/file?rel=` serves a captured relative figure from a submitted gated
  document. It does not read the live checkout; a direct file request or an
  uncaptured relative file returns `404 content_unavailable`.

History contains observed version metadata. Deleting a resource marks its
record deleted and removes its associations, but preserves version history.

## Reviews

```http
GET  /api/projects/{project_id}/reviews
GET  /api/projects/{project_id}/reviews?target_type={type}&target_id={id}
POST /api/projects/{project_id}/reviews/request
POST /api/projects/{project_id}/reviews/start
POST /api/projects/{project_id}/reviews/submit
```

Roles are `design_reviewer`, `experiment_reviewer`, `reflection_reviewer`,
`human`, and `automated_check`; verdicts are `pass`, `needs_changes`, and
`fail`.

A fresh review request returns the plaintext `reviewer_capability` and a ready
`reviewer_handoff.spawn_prompt`. The server stores only its hash, so the token
cannot be recovered later from workflow status. Reviewer agents normally use
the equivalent MCP surface rather than browser routes.

Workflow review substates currently include `none`, `requested`, `started`, and
`attested_blocked` (a verified review is required).

## Sandboxes

The browser observes sandbox state, terminal output, and live metrics. It may
release a sandbox after an explicit UI confirmation, but procurement,
attachment, command execution, output pulls, and extension remain agent/MCP
operations.

```http
GET  /api/sandboxes/health
GET  /api/projects/{project_id}/sandboxes
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox
GET  /api/projects/{project_id}/sandboxes/{sandbox_uid}
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox/metrics
GET  /api/projects/{project_id}/sandboxes/{sandbox_uid}/metrics
GET  /api/projects/{project_id}/experiments/{experiment_id}/sandbox/terminal
GET  /api/projects/{project_id}/sandboxes/{sandbox_uid}/terminal
POST /api/projects/{project_id}/experiments/{experiment_id}/sandbox/release
POST /api/projects/{project_id}/sandboxes/{sandbox_uid}/release
```

Use `sandbox_uid` routes when an experiment has multiple sandboxes. Terminal
responses include `transcript`, an absolute byte `cursor`, command status
fields, and a `running` flag. Pass the previous cursor as `since=` for an
incremental read; use `tail=` for the initial bounded read.

Metrics are sampled on demand through the brain's management SSH channel and
are best-effort. They include CPU, memory, and GPU utilization when available.
Repeated reads are coalesced briefly by the transcript and metrics caches.

HTTP sandbox rows omit checkout-local paths and caller private-key details.
Everything left on a sandbox is destroyed at release or expiry; retained light
outputs must first be pulled by the proxy and heavy outputs uploaded through
storage tools.

## Durable heavy storage

These routes are functional only when an object-store backend is configured:

```http
GET    /api/projects/{project_id}/storage
GET    /api/projects/{project_id}/storage/{object_id}
POST   /api/projects/{project_id}/storage/{object_id}/download
POST   /api/projects/{project_id}/storage/{object_id}/pin
POST   /api/projects/{project_id}/storage/{object_id}/unpin
POST   /api/projects/{project_id}/storage/{object_id}/renew
DELETE /api/projects/{project_id}/storage/{object_id}
```

The UI manages object lifecycle and requests short-lived download links. Local
file upload and download execution belongs to the proxy's storage tools.

## Research feed

```http
GET  /api/projects/{project_id}/feed?limit={n}&cursor={created_seq}
POST /api/projects/{project_id}/feed/{post_id}/reactions
POST /api/projects/{project_id}/feed/{post_id}/reply
GET  /api/projects/{project_id}/feed/{post_id}/image
GET  /api/projects/{project_id}/feed/{post_id}/link-image
GET  /api/projects/{project_id}/feed/{post_id}/embed
POST /api/projects/{project_id}/feed/track
```

Agent posts and local image/embed capture use MCP plus proxy data-plane
submissions. Browser mutations are limited to researcher reactions, replies,
and UI telemetry.

## Activity and tool-I/O diagnostics

```http
GET  /api/activity?limit=100&source={mcp|http|app}&project_id={project_id}
GET  /api/debug/tool-calls?minutes=&source=&status=&tool=&project_id=&limit=&sort=&order=
GET  /api/debug/tool-calls/{call_id}
POST /api/debug/tool-calls/clear
```

These are diagnostic rings, not durable research records:

- activity keeps up to 5,000 summarized events in process memory;
- tool-I/O keeps up to 1,500 full request/response records in process memory;
- both reset on brain restart;
- capability fields are redacted before they are exposed.

The durable research timeline is `GET /api/projects/{project_id}/events`, whose
rows are committed with accepted state changes.

Because there is no current authentication boundary, the diagnostic and clear
routes are private-operator surfaces and are not tenant-isolated.

## Errors

Domain errors use JSON with `detail` and `error_code`. Missing records and
unavailable file bytes return 404; invalid requests and rejected workflow
operations return 400; a below-floor hosted client returns 426. Local mode also
rejects browser requests carrying a non-loopback `Origin`.

The `/mcp/*`, `/api/data-plane/*`, and `/api/admin/*` route families are not
browser UI APIs. They are respectively the proxy control transport,
proxy-to-brain submissions, and private operator endpoints.
