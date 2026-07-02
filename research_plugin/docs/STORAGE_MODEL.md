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

## Mental model

The backend keeps a **ledger** of named aliases. Each alias points at a physical,
content-addressed object (`sha256`) living in a bucket. The control plane records
the ledger and mints presigned URLs; it never moves bytes. The **producer** —
a GPU sandbox for a model it trained, or the local daemon for a precious local
dataset — computes the file's `sha256` and streams the bytes directly to the
bucket through a presigned URL. The user's machine is never in the byte path.

Nothing is automatic. An object lands in storage only because the agent decided a
file is worth keeping and called `storage.put_object`; a sandbox only receives an
object when the agent explicitly fetches it. See "Saving" in
[future_features/heavy_file_storage.md](../../dev_docs/future_features/heavy_file_storage.md).

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
  "expires_at": "2026-08-24T14:21:03Z",   // null = pinned (kept forever)
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
auto-increments per `(project_id, name)`; re-registering the same `name`+`sha`
is idempotent and does not bump it.

## Identity & dedup (locked decisions)

- **Content-addressed + named alias.** `sha256` keys the bytes; the ledger holds
  the human name/version.
- **Soft provenance.** The producing experiment/run are plain strings — no
  foreign keys, no imports from the experiments domain. Storage stays standalone.
- **The ledger owns lifecycle.** `expires_at` lives on the ledger row; the
  object provider only stores, stats, presigns, and deletes bytes.

## TTL / lifecycle

- New / just-completed objects get `expires_at = now + 60 days`.
- **Access auto-extends:** `storage.resolve` (the download path) resets the clock
  to `now + 60d`, extend-only — it never shortens.
- **Pin** clears `expires_at` (kept forever); **unpin/renew** restore a 60-day
  deadline.
- **Sweep:** `CleanupService` calls `sweep_expired`, which marks due rows
  `expired` and reclaims the physical object **only when the last active alias for
  a sha is gone** (refcount).

## Operations (`storage.*` MCP tools)

Project-scoped; the server rejects a missing `project_id`.

- `storage.put_object(project_id, name, kind, sha256, size_bytes, content_type?, producing_experiment_id?, producing_run?, source_uri?, notes?)`
  — register intent. Returns `{deduped, object}` when the bytes already exist
  (no upload), `{object, idempotent}` for an identical re-register, else
  `{object, upload}` with a presigned (multipart) upload target.
- `storage.upload_file(project_id, path, kind, name?, content_type?, producing_experiment_id?, producing_run?, source_uri?, notes?)`
  — data-plane convenience helper for local agents. Computes sha256 + size,
  registers the intent, streams the file to the presigned target, and completes
  the upload. Relative paths resolve under the project repo; omitted `name`
  defaults to the repo-relative path.
- `storage.complete_upload(project_id, upload_id, parts?)` — finalize after the
  producer streamed bytes: verifies size + sha256, lands the object, sets
  `available` + a 60-day TTL.
- `storage.list(project_id, kind?, name?, status?, include_expired?, limit?, offset?, compact?)` — browse the ledger.
- `storage.resolve(project_id, object_id? | name?, version?, include_download?)` — resolve to a presigned **download** URL; bumps the TTL.
- `storage.download_file(project_id, path, object_id? | name?, version?, overwrite?)`
  — data-plane convenience helper. Resolves the object, downloads to a temp
  file, verifies sha256 + size, then atomically replaces `path`.
- `storage.pin / storage.unpin / storage.renew (project_id, object_id)`.
- `storage.delete(project_id, object_id)` — drop the alias (kept for audit);
  reclaim the physical bytes when no alias references them.

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
  parameterized by `endpoint_url` + region (boto3 lazy-imported). Multipart
  presigned upload above a threshold; integrity verified via the
  `x-amz-checksum-sha256` trailer (no GB re-hash).

Storage is optional and disabled when `RESEARCH_PLUGIN_STORAGE_PROVIDER` is
unset; disabled backends do not advertise `storage.*` tools. Enable it with
`RESEARCH_PLUGIN_STORAGE_PROVIDER=s3` plus `RESEARCH_PLUGIN_STORAGE_BUCKET`,
`RESEARCH_PLUGIN_STORAGE_ENDPOINT_URL`, `RESEARCH_PLUGIN_STORAGE_REGION`,
`RESEARCH_PLUGIN_STORAGE_ACCESS_KEY_ID`, and
`RESEARCH_PLUGIN_STORAGE_SECRET_ACCESS_KEY`. The storage credential vars are
storage-specific and fall back to `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
when unset. Local non-cloud deployments should run MinIO and point these env
vars at it. Users bring their own S3-like storage by setting these — no code
change.

## State layout

```text
.research_plugin/
  state.sqlite          # the storage_objects ledger (alongside projects/experiments)
```

Hosted control keeps the ledger in Postgres and the bytes in the configured
bucket (R2/S3/MinIO). Namespaces are project-scoped today; objects are never
deduplicated across namespaces (cross-tenant dedup would leak content
existence).

## Rules

- Identity is `(project_id, name, version)`; physical bytes are shared by `sha256`.
- The control plane never moves bytes — producers PUT/GET via presigned URLs.
- Deleting an alias keeps its ledger row (audit) and reclaims bytes only when
  unreferenced.
- A reaped sandbox does NOT auto-save its outputs — saving is always an explicit
  `storage.put_object`. Storage is decoupled from sandbox provisioning.
