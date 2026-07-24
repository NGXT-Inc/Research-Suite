# Storage Model

## Goal

Preserve heavy files that neither git nor rsync should carry:

> A storage object is a heavy file (a dataset or a trained model) kept off-repo
> in S3-compatible object storage, content-addressed and tracked by a
> project-level ledger.

Git carries the scripts; long-term storage preserves the bytes. This is the home
for **precious datasets** (hard to re-download) and **trained models worth
keeping** (e.g. a base model fine-tuned many ways later). It is architecturally
separate from the rest of the product — the rest of the system reaches it only
through a thin, one-directional bridge.

## What Belongs Where

Rule of thumb: use storage for files that are too large or noisy for repo
the repo or artifact submission, or expensive to regenerate.

- Use storage for checkpoints/model weights, precious datasets, dataset shards,
  parquet/archive outputs, generated caches that must survive, and logs/traces
  over about 10 MB that a reviewer may need.
- Keep repo files (and submitted artifacts) for `plan.md`, `report.md`, `graph.json`, scripts,
  configs, small retained result files, metrics TSV/JSON, summarized logs, and
  plots referenced by the report.
- Leave ephemeral on the sandbox when the file is a regenerable package cache,
  scratch download, temporary preprocessing output, or large intermediate not
  needed after the run.

## Mental model

The brain keeps a **ledger** of named aliases. Each alias points at a physical,
content-addressed object (`sha256`) living in a bucket. The brain records the
ledger and mints presigned URLs; it never proxies the object bytes. On the
agent-facing path the agent computes the checksum, calls the tool, and runs the
one-line command it returns, which streams the file directly to or from the
object store.

Nothing is automatic. An object lands in storage only because the agent decided a
file is worth keeping and called `storage.submit`; downloading is equally
explicit. Sandbox files must be retained before the sandbox is released or
expires. `storage.submit` returns a `run` command the agent executes to PUT the
bytes straight to storage — they never pass through the brain or the agent's
context.

## Core shape

```json
{
  "id": "sto_...",
  "project_id": "proj_...",
  "name": "imagenet-subset",
  "version": 2,
  "kind": "dataset | model | other",
  "content_sha256": "…64 hex…",
  "size_bytes": 4823195012,
  "content_type": "application/x-tar",
  "namespace": "proj_...",
  "status": "uploading | completing | available | expired | deleted",
  "expires_at": "2026-08-24T14:21:03Z",   // null while uploading or when pinned
  "created_by": "codex | user",
  "producing_experiment_id": "exp_...",     // soft provenance — plain strings
  "producing_run": "",
  "source_uri": "",
  "notes": "",
  "created_at": "…",
  "updated_at": "…",
  "last_accessed_at": "…"
}
```

Identity has two layers: the **physical object** is content-addressed by
`content_sha256` within the namespace (so identical bytes are stored once); the
**alias** is the `(name, version)` ledger row. Many aliases may point at one
physical object — that is how a reused base model is stored once. `version`
auto-increments per `(project_id, name)`. Re-registering the same `name`+`sha`
is idempotent only when the matching ledger row is already `available`;
non-available historical rows do not suppress a new version.

## Identity & dedup (locked decisions)

- **Content-addressed + named alias.** `sha256` keys the bytes; the ledger holds
  the human name/version.
- **Soft provenance.** The producing experiment/run are plain strings — no
  foreign keys, no imports from the experiments domain. Storage stays standalone.
- **The ledger owns lifecycle.** `expires_at` lives on the ledger row; the
  object provider only stores, stats, presigns, and deletes bytes.

## TTL / lifecycle

- A new upload intent has no expiry while it is `uploading`. Completion assigns
  `expires_at = now + 60 days`; a deduplicated alias created directly as
  `available` receives the same deadline.
- **Access auto-extends:** every `storage.find` resolve-mode call resets the
  clock to `now + 60d`, extend-only — it never shortens. `include_download`
  controls whether the response also contains a presigned URL, not the access
  update.
- **Pin** clears `expires_at` (kept forever); **unpin/renew** restore a 60-day
  deadline.
- **Sweep:** `CleanupService` calls `sweep_expired`, which marks due rows
  `expired` and reclaims the physical object **only when the last active alias for
  a sha is gone** (refcount).

## Operations (`storage.*` MCP tools)

Project-scoped; a keyed agent's project is fixed by its key, while brain and HTTP
calls remain explicitly scoped. Agents see four tools — `storage.submit`,
`storage.fetch`, `storage.find`, and `storage.object`. Two lower-level primitives
(`storage.put_object`, `storage.complete_upload`) stay dispatchable for the
manual presign path but are hidden from the agent-facing `tools/list`
(`MCP_HIDDEN_TOOL_NAMES`); `storage.submit` composes them server-side to mint the
returned command.

Agent-facing:

- `storage.submit(path, kind, sha256, size_bytes, name?, content_type?, producing_experiment_id?, producing_run?, source_uri?, notes?)`
  — the agent computes the file's sha256 + size and calls this; the brain
  registers the intent and returns a one-line `run` command (a presigned
  `curl -T` PUT followed by a one-time completion callback). The agent runs it
  verbatim to stream the bytes straight to storage; omitted `name` defaults to
  the given path. Bytes never transit the brain or the agent context.
- `storage.fetch(path, object_id? | name?, version?)`
  — resolves the object and returns a one-line `run` command (a presigned
  `curl -o`) that downloads the bytes to `path` and verifies the stored sha256.
- `storage.find(object_id? | name?, version?, include_download?, kind?, status?, include_expired?, limit?, offset?, compact?)`
  — **resolve mode** (pass `object_id` or `name`): resolve one object to its
  ledger row and bump its TTL; with `include_download=true`, also return a
  presigned **download** URL. **List mode** (omit both): browse the ledger, filtered by
  `kind` / `status`, paginated with `limit` / `offset`, `compact=true` for a lean
  projection.
- `storage.object(object_id, action)` — apply a lifecycle
  `action` to one object: `pin` (expiry cleanup keeps it), `unpin` (restore its
  default expiry), `renew` (renew its default expiry window), or `delete` (drop
  the alias, kept for audit; reclaim the physical bytes when no active alias
  references them).

Hidden primitives (manual presign path):

- `storage.put_object(project_id, name, kind, sha256, size_bytes, content_type?, producing_experiment_id?, producing_run?, source_uri?, notes?)`
  — register intent. Returns `{object, idempotent}` for an identical available
  `name`+SHA row, `{deduped, object}` when the physical bytes already exist but
  a new alias/version is needed, or `{object, upload}` with a presigned
  (multipart) upload target.
- `storage.complete_upload(project_id, upload_id, parts?)` — finalize after the
  producer streamed bytes, apply the provider-specific verification available,
  and set `available` plus a 60-day TTL. S3 single-part completion requires a
  matching provider `ChecksumSHA256` and enforces the declared size as a maximum;
  multipart completion checks exact size but does not re-download the object to
  recompute SHA-256.

## HTTP surface (read + lifecycle, for the UI)

The UI browses and manages lifecycle; it never uploads (bytes are agent-driven).

- `GET    /api/projects/{pid}/storage` — list (filters: kind, status, name, include_expired)
- `GET    /api/projects/{pid}/storage/{id}` — side-effect-free detail (no TTL bump)
- `POST   /api/projects/{pid}/storage/{id}/download` — presigned URL + TTL bump
- `POST   /api/projects/{pid}/storage/{id}/{pin|unpin|renew}`
- `DELETE /api/projects/{pid}/storage/{id}`

## Provider (plug-and-play)

One `ObjectStore` port; one production implementation:

- `S3CompatibleObjectStore` — one class for **Cloudflare R2, AWS S3, and MinIO**,
  parameterized by `endpoint_url` + region (boto3 lazy-imported). Large files
  use multipart presigned uploads. Completion uses the provider metadata and
  checksum guarantees described above; it never re-downloads multi-gigabyte
  objects to recompute their checksum.

Storage is optional and disabled when `MERV_STORAGE_PROVIDER` is
unset; disabled backends do not advertise `storage.*` tools. The required S3
settings are `MERV_STORAGE_PROVIDER=s3` and
`MERV_STORAGE_BUCKET`. Endpoint and region are provider-dependent.
Credentials may come from the storage-specific variables, the standard `AWS_*`
variables, or boto's normal credential chain. Local non-cloud deployments can
run MinIO and set an endpoint URL. Users bring their own S3-like storage by
configuration; no code change is required.

## State placement

The ledger lives with the rest of the brain's research records: SQLite for a
local brain or Postgres for a hosted brain. Object bytes live in the configured
S3-compatible bucket (R2, S3, or MinIO). Namespaces are project-scoped; objects
are never deduplicated across namespaces because cross-tenant deduplication
would leak content existence.

## Rules

- Identity is `(project_id, name, version)`; physical bytes are shared by `sha256`.
- The brain never proxies object bytes — producers PUT/GET via presigned URLs.
- Deleting an alias keeps its ledger row (audit) and reclaims bytes only when
  no active (`uploading`, `completing`, or `available`) alias references them.
- A reaped sandbox does NOT auto-save its outputs — saving is always an explicit
  `storage.submit`. Storage is decoupled from sandbox provisioning.
