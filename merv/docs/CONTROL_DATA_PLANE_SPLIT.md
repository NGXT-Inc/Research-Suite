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
`RESEARCH_PLUGIN_CONTROL_URL` env var > machine config from
`merv-client configure` > the hosted default. Local deployments
configure `http://127.0.0.1:8787`. There is no marker discovery and no thin
local upstream path.

## Brain responsibilities

| Component | Modules | Why brain-side |
|---|---|---|
| Projects, claims, experiments, reviews, reflections | `services/*` | Durable records and workflow policy. |
| Workflow gates | `services/workflow.py`, validators | Pure policy over submitted records/bytes. |
| Sandbox lifecycle | `services/sandbox/*`, `execution/backends/*` | Provider credentials, VM lifecycle, quotas, reapers. |
| State and audit | `state/*` | SQLite locally, Postgres/durable stores hosted. |
| Blob/storage records | `storage/*`, blob stores | Submitted artifacts and optional heavy objects sent explicitly through data-plane flows. |
| UI/API surface | `transport/api/*` | Browser/control endpoints plus private proxy-submission routes under `/api/data-plane/*`; browsers do not perform checkout-local operations. |

The brain may serve `/api/*` for the UI and `/mcp/*` for control-tool calls, but
repo bytes enter only through explicit proxy-local tool submissions. It does not
inspect paths under a checkout.

## Proxy-local data-plane responsibilities

| Component | Modules | Why proxy-side |
|---|---|---|
| Resource observation | `dataplane/resource_observer.py`, `resource_validation.py`, `resource_artifacts.py` | Reads repo files, hashes bytes, captures gated artifacts. |
| Experiment folders | `dataplane/experiment_folders.py` | Creates `experiments/<name>/` in the checkout. |
| Feed attachments | `dataplane/feed_images.py`, `dataplane/feed_embeds.py` | Reads local images and HTML embeds submitted with feed posts. |
| Resource paths | `dataplane/repo_paths.py` | Normalizes and bounds checkout-relative paths. |
| Storage transfer | `storage/file_transfer.py`, called by `mcp_server/local_data_plane.py` | Hashes and transfers local checkout files through presigned object-store URLs. |
| Sandbox output pulls | `dataplane/sandbox_outputs.py`, lazy-imported by `mcp_server/local_data_plane.py` | Runs safe `rsync` from the sandbox into the local experiment folder. |
| Project links | `mcp_server/project_links.py` | Maps checkout folders to brain project ids in `project_links.sqlite`. |
| Caller SSH custody | client/proxy environment | `sandbox.request` requires caller `public_key`; the caller owns the private key and supplies its path only for local rsync operations. |

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

`sandbox.pull_outputs` is proxy-local in every deployment. It asks the brain for
the current sandbox record, uses the caller's private key path supplied by the
client/proxy side, and reuses the safe rsync logic from
`dataplane/sandbox_outputs.py`. Heavy artifacts should still go through durable
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
| Hosted (default) | `https://experiments.rapidreview.io` | durable DB + submitted-byte blob store; optional heavy-object store | private operator surface; no end-user auth | same thick data plane |
| Local | `http://127.0.0.1:8787` | SQLite + local-dir blobs | no auth; foreign browser origins rejected | same thick data plane |

`RESEARCH_PLUGIN_MODE` names the preset used to start the brain. It does not
create a second composition path.

CORS and the client-version floor are not authentication. Until an end-user
authentication layer exists, hosted control must remain behind trusted network
and operator access controls.

## Related

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — product and process architecture.
- [`STARTUP_CHEATSHEET.md`](STARTUP_CHEATSHEET.md) — localhost brain startup.
- [`CONTROL_PLANE_OPERATIONS.md`](CONTROL_PLANE_OPERATIONS.md) — hosted brain operations.
