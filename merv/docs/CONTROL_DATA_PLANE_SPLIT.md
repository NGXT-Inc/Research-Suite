# Brain / data-plane split

## Rule

> The brain never reads a user's checkout and never owns caller private keys.

That rule is true for both deployments. Local deployment is not a different
topology: it is the same brain composition running on localhost with small
deployment defaults (SQLite, local-dir blobs, local management keys, no auth,
and a local-origin browser guard). Hosted deployment points the same stdio
proxy at a hosted brain URL.

## Runtime shape

```text
USER MACHINE
  Agent client
      │ stdio
      ▼
  merv-mcp
      ├─ local data tools: repo reads, hashes, validation, folder mkdir,
      │                    rsync output pulls, project_links.sqlite
      └─ control tools/data submissions over HTTP
                              │
                              ▼
                         Brain service
                         hosted: https://experiments.rapidreview.io (default)
                         local:  http://127.0.0.1:8787
```

The proxy always dials one brain URL, resolved as the
`MERV_CONTROL_URL` env var > machine config from
`merv-client configure` > the hosted default. Local deployments
configure `http://127.0.0.1:8787`. There is no marker discovery and no thin
local upstream path.

## Brain responsibilities

| Component | Modules | Why brain-side |
|---|---|---|
| Projects, claims, experiments, reviews, reflections | `research_core/*` | Durable records and workflow policy. |
| Workflow gates | `research_core/next_action.py`, validators | Pure policy over a bulk Research snapshot and Sandbox read facade. |
| Sandbox lifecycle | `sandbox/*`, `sandbox/execution/backends/*` | Provider credentials, VM lifecycle, quotas, reapers. |
| State and audit | `kernel/state/*` | SQLite locally, Postgres/durable stores hosted. |
| Blob/storage records | `object_storage/*`, blob stores | Submitted artifacts and optional heavy objects sent explicitly through data-plane flows. |
| UI/API surface | `transport/api/*` | Browser/control endpoints plus private proxy-submission routes under `/api/data-plane/*`; browsers do not perform checkout-local operations. |

The brain may serve `/api/*` for the UI and `/mcp/*` for control-tool calls, but
repo bytes enter only through explicit proxy-local tool submissions. It does not
inspect paths under a checkout.

## Proxy-local data-plane responsibilities

| Component | Modules | Why proxy-side |
|---|---|---|
| Resource observation | `src/merv/proxy/dataplane/resource_observer.py`, `src/merv/proxy/dataplane/resource_artifacts.py` | Reads repo files, hashes bytes, captures gated artifacts. |
| Experiment folders | `src/merv/proxy/dataplane/experiment_folders.py`, `src/merv/proxy/workspace.py` | Creates `experiments/<name>/` in the checkout. |
| Feed attachments | `src/merv/proxy/dataplane/feed_images.py`, `src/merv/proxy/dataplane/feed_embeds.py` | Reads local images and HTML embeds submitted with feed posts. |
| Resource paths | `src/merv/proxy/dataplane/repo_paths.py` | Normalizes and bounds checkout-relative paths. |
| Storage transfer and guidance | `src/merv/shared/file_transfer.py`, `src/merv/shared/storage_guidance.py`, called by `src/merv/proxy/local_data_plane.py` | Hashes and transfers local checkout files through presigned object-store URLs while sharing stable guidance with the brain. |
| Sandbox output pulls | `src/merv/proxy/dataplane/sandbox_outputs.py` | Runs safe `rsync` from the sandbox into the local experiment folder. |
| Project links | `src/merv/proxy/project_links.py` | Maps checkout folders to brain project ids in `project_links.sqlite`. |
| Caller SSH custody | client/proxy environment | `sandbox.request` requires caller `public_key`; the caller owns the private key and supplies its path only for local rsync operations. |

The proxy has a hard zero-brain-import invariant, for both ordinary and dynamic
imports. It may import only the standard library, `merv.proxy`, and
dependency-free `merv.shared`; shared code imports neither plane. Stable wire
shape checks such as OpenSSH public-key and resource-register mode validation
are shared, while authoritative artifact and workflow policy validation occurs
on brain submission endpoints before any mutation. The former proxy-side
`resource_validation.py` helper was deleted because it had no production
consumer.

## Tool split

Control tools go to the brain. Data tools run in the proxy and submit explicit
facts or bytes to the brain:

- `experiment.materialize_folders`
- `resource.register` (register file(s) + optionally associate + capture bytes)
- `storage.upload_file` and `storage.download_file`
- `sandbox.request`, `sandbox.attach`, and `sandbox.pull_outputs`
- `feed.post` (captures an optional local image or HTML embed before recording
  the post)
- `project` with `action: "connect"` — served by the proxy process itself: it
  validates (or creates) the project on the brain, then writes the
  folder→project link to `project_links.sqlite`. The one call where `project_id`
  is caller-authoritative rather than link-resolved. (`action: "current"` is
  also proxy-served; `action: "create"` forwards to the brain.)

`surface/tools/contracts.py::TOOL_MANIFEST` is the single authored registry for
every tool. Each entry owns its schema and description, public/internal
visibility, project-scope strategy, execution strategy, optional feature
requirements, and handler identity. The legacy plane sets, hidden set, handler
registry, public catalog, and the stdlib proxy's private routing manifest are
derived projections. `scripts/regen_tool_catalog.py --check` prevents either
checked-in JSON projection from drifting.

The local proxy is split by responsibility: `routing.py` makes pure dispatch
decisions, `http_client.py` owns JSON transport and error translation,
`credential_provider.py` owns hosted session refresh, `project_scope.py` owns
folder links, `mcp_shell.py` owns protocol framing, and `proxy.py` is the small
composition edge. Composite
`sandbox.get` performs one control read and passes those facts into its local
enricher, so the merge does not repeat the brain lookup.

`sandbox.pull_outputs` is proxy-local in every deployment. It asks the brain for
the current sandbox record, uses the caller's private key path supplied by the
client/proxy side, and reuses the safe rsync logic from
`src/merv/proxy/dataplane/sandbox_outputs.py`. Heavy artifacts should still go through durable
storage tools instead of being copied into the repo.

## Sandbox key custody

`sandbox.request` requires `public_key` everywhere. The brain authorizes that
public key with the provider and stores `public_key_source: "caller"` for new
requests. Legacy rows with `public_key_source: "managed"` remain readable,
releasable, terminable, and reattachable; the request path no longer mints or
writes managed user keypairs. Management/transcript keys are separate and remain
brain-side operational credentials.

## Deployment presets

| Deployment | Brain URL | State/blob defaults | Auth/CORS | Proxy role |
|---|---|---|---|---|
| Hosted (default) | `https://experiments.rapidreview.io` | durable DB + submitted-byte blob store; optional heavy-object store | Supabase-backed end-user auth (required in production via `MERV_REQUIRE_AUTH=1`) | same thick data plane |
| Local | `http://127.0.0.1:8787` | SQLite + local-dir blobs | auth off by default; foreign browser origins rejected | same thick data plane |

`MERV_MODE` names the preset used to start the brain. It does not
create a second composition path.

End-user authentication is Supabase-backed and optional: unset locally (an
unauthenticated hosted boot logs an "OPEN" warning), enforced on the hosted
deployment where `MERV_REQUIRE_AUTH=1` turns missing auth config into a
startup failure (see [`AUTH.md`](AUTH.md)). CORS and the client-version floor
are still not authentication, and hosted control stays behind TLS and
operator access controls regardless.

## Related

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — product and process architecture.
- [`STARTUP_CHEATSHEET.md`](STARTUP_CHEATSHEET.md) — localhost brain startup.
- [`CONTROL_PLANE_OPERATIONS.md`](CONTROL_PLANE_OPERATIONS.md) — hosted brain operations.
