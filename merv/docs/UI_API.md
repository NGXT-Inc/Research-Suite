# Browser HTTP API

The Merv UI talks directly to the brain's `/api/*` HTTP surface. The
same brain also serves `/mcp`, the universal MCP transport every agent client
connects to directly, but the browser is not an agent runtime and has no chat
endpoint.

The route modules under `src/merv/brain/surface/transport/api/` and the projections in
`src/merv/brain/surface/transport/api/views.py` are the executable source of truth for this
document.

## Runtime and trust boundary

- Project scope is explicit in the URL. The UI selects a `project_id` and uses
  `/api/projects/{project_id}/...`; the brain does not infer a current project.
- The brain never receives a checkout root and never reads a user's checkout;
  agents never send `repo_root`. Any checkout-local work stays on the agent
  client itself.
- The supported browser surface does not provision sandboxes, pull sandbox
  outputs, or upload/download storage files. Those operations run through MCP
  tools that hand the agent a one-line command to move bytes over a presigned URL.
- Local and hosted brains expose the same HTTP shape. Local mode normally uses
  SQLite and local blobs; control mode uses operator-configured durable stores.
- Authentication is deployment-conditional. Auth-off deployments (the local
  default) run every request as the implicit `local` principal, and must remain
  on a trusted network. Hosted control with auth configured enforces Supabase
  end-user sessions on `/api/*` — with `project_members` isolation (foreign
  projects 404) — plus `rr_sk_` API keys and project-scoped `mk_` keys at the
  gateway. CORS restrictions are not authentication.

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
  "server_version": "0.0013",
  "min_proxy_version": "0.0013",
  "catalog_version": "2026-07-24",
  "mode": "local",
  "auth": {"required": false},
  "capabilities": {
    "hosted_control": false,
    "mcp": true,
    "token_uploads": true
  }
}
```

The `capabilities` block reports `mcp: true` and `token_uploads: true`: every
agent client connects over the shared `/mcp` HTTP transport, and bytes move over
returned presigned token commands rather than through the brain. `catalog_version`
is a deployment-drift marker for the MCP tool catalog. `min_proxy_version` is the
retained minimum legacy-client floor: in control mode a request carrying an
`X-RP-Client-Version` explicitly below it receives `426 client_too_old`, and the
floor will be retired once telemetry shows no old clients remain. A missing
version header is currently tolerated.

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

Create projects with `name` and `summary`. Do not send a repo path: projects are
never tied to a checkout, and each agent key binds one immutable project.

`/home` is the primary UI bootstrap. It returns `project`, `claims`, the full
`experiments` list, `artifacts`, `reviews`, `recent_events`, `stats`, `workflow`,
`active_experiment`, `active_experiments`, `active_processes`, and MLflow health.
`active_experiments` contains non-terminal work with its workflow, sandboxes,
and active processes. `active_processes` includes both `provisioning` and
`running` sandboxes.

## Claims

```http
GET   /api/projects/{project_id}/claims
POST  /api/projects/{project_id}/claims
GET   /api/projects/{project_id}/claims/{claim_id}
PATCH /api/projects/{project_id}/claims/{claim_id}
PUT   /api/projects/{project_id}/claims/{claim_id}
```

Claim creation accepts `statement`, `scope`, and `confidence`. Updates use the
same control-plane validation as MCP claim mutations.

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
experiments and provides dashboard links when configured.

## Literature review

```http
GET /api/projects/{project_id}/litreview
```

The whole living review in one read: `summary` (synthesized with
`exists: false` until first written), ordered `sections` with cited papers,
and the `papers` ledger with its links. Served with a content-hash `ETag`
and honors `If-None-Match` (304).

## Reflections

```http
GET /api/projects/{project_id}/reflections
GET /api/projects/{project_id}/reflections/{reflection_id}
GET /api/projects/{project_id}/reflections/current/graph
GET /api/projects/{project_id}/reflections/{reflection_id}/graph
```

These are the canonical reflection-wave paths; response keys and the stored
review-stage status (`reflection_review`) use the reflection vocabulary.

The overview returns full wave states, the open/latest wave, and the project
reflection staleness signal. A wave includes its five-lens roster, corpus,
attempt-scoped artifacts, reviews, lens coverage, gate checklist, graph diff,
and allowed transitions. Per-wave graph reads use the artifact pinned by that
wave, so historical graphs remain faithful after the living graph changes.

## Artifacts

```http
GET /api/projects/{project_id}/artifacts
GET /api/projects/{project_id}/artifacts?target_type={type}&target_id={id}&role={role}
GET /api/projects/{project_id}/artifacts/{artifact_id}/content
GET /api/projects/{project_id}/artifacts/{artifact_id}/file
GET /api/projects/{project_id}/artifacts/{artifact_id}/figure?rel={relative_path}
```

There are deliberately no browser submission routes: artifacts are submitted
by the agent (`artifact.submit` plus the returned one-time-token
`PUT /api/artifacts/u/{token}` upload; figures via
`PUT /api/artifacts/f/{token}`). The listing returns compact complete-artifact
rows (id, target, role, attempt, lens_id, path label, title, size,
timestamps).

Content behavior:

- `/content` serves the pinned submitted bytes; a pending artifact returns
  `available: false`.
- `/file` serves the raw bytes with the path label's filename.
- `/figure?rel=` serves a submitted figure for a markdown artifact; an
  unsubmitted link returns 404. Nothing ever reads a live checkout.

Each artifact is one immutable submission: resubmitting a slot mints a new
artifact id and deletes the superseded row.

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
outputs must first be pulled by the agent (the `sandbox.pull_outputs` tool
returns an rsync command the agent runs) and heavy outputs uploaded through
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

The UI manages object lifecycle and requests short-lived download links. File
upload and download execution belongs to the agent's storage tools
(`storage.submit` / `storage.fetch`), which return a presigned curl command the
agent runs to stream bytes directly to or from the object store.

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

Agent posts and image/embed capture use MCP tools that return a presigned upload
command the agent runs. Browser mutations are limited to researcher reactions,
replies, and UI telemetry.

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

In auth-off deployments there is no authentication boundary, so the diagnostic
and clear routes are private-operator surfaces and are not tenant-isolated.

## Errors

Domain errors use JSON with `detail` and `error_code`. Missing records and
unavailable file bytes return 404; invalid requests and rejected workflow
operations return 400; a below-floor hosted client returns 426. Local mode also
rejects browser requests carrying a non-loopback `Origin`.

The `/mcp` and `/api/admin/*` route families are not browser UI APIs. They are
respectively the universal MCP transport that every agent client connects to and
the private operator endpoints.
