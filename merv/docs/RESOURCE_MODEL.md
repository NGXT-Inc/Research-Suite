# Resource Model

## Definition

> An agent-authored resource is a regular file in a research checkout that the
> local MCP proxy has explicitly registered with the brain. The one current
> system-authored exception is a brain-created metrics exhibit, represented as
> a resource with pinned bytes but no corresponding checkout file.

Checkout files may exist and change without becoming research resources.
Registration is the boundary that records why a particular observed file
version matters to a claim, experiment, review, reflection, or attempt.

The proxy reads checkout files; the brain does not. The proxy normalizes and
bounds the repo-relative path, computes metadata and a content hash, performs
local artifact checks needed to capture submitted bytes, and sends the
observation to the brain. The brain authoritatively validates the target and
association role.

## Durable records

One active resource exists per `(project_id, path)`. The brain stores:

- a stable `resource_id` and repo-relative `path`;
- kind, title, creator, missing/deleted state, and current version;
- append-only resource versions with size, mtime, SHA-256, content type, and
  observation time;
- associations to targets, roles, attempts, and exact version ids.

An experiment association is attempt-scoped. Resources from older attempts
remain visible as history but cannot satisfy the current attempt's gates.

A representative resource is:

```json
{
  "id": "res_...",
  "project_id": "proj_...",
  "path": "experiments/baseline/results/metrics.json",
  "kind": "result",
  "current_version_id": "rver_...",
  "version_token": "experiments/baseline/results/metrics.json:1789520738123456789:1789520738123456799:42183",
  "missing": 0,
  "current_version": {
    "id": "rver_...",
    "content_sha256": "...",
    "content_type": "application/json",
    "size_bytes": 42183,
    "mtime_ns": 1789520738123456789
  },
  "associations": [
    {
      "target_type": "experiment",
      "target_id": "exp_...",
      "role": "result",
      "attempt_index": 2,
      "version_id": "rver_..."
    }
  ]
}
```

The live path is convenient identity; `resource_versions.id` is the immutable
historical identity used by associations and review snapshots.

## Submitted bytes

Most resources are metadata-only. Historical content for those files remains
the user's responsibility through the working tree, git history, or durable
object storage.

The brain stores bytes for the narrow cases that must remain immutable after
submission:

- **Gated artifacts** — plans, reports, experiment graphs, project graphs,
  reflection lens documents, reflection documents, and change specs. Each role
  has a size cap. Workflow lints and reviewers read the pinned submission, not a
  later working-tree edit.
- **Small metric result JSON** — `metrics.json`, `results.json`, and JSON files
  under `results/`, up to 16 KB, may be captured so the system metrics exhibit
  can ingest them.
- **Metrics exhibits** — at `submit_results` the brain evaluates the
  system-authored exhibit. It pins the exhibit when attempt-window runs are
  found, or when MLflow is unavailable after a plugin-created run established
  quantitative intent. Qualitative/no-run attempts get no exhibit. Agents
  cannot create or replace this role.

Changing a submitted file does not update the pinned version. Fix the file and
call `resource.register` again to submit the revision.

## Version identity

The lightweight observation token is:

```text
path + mtime_ns + ctime_ns + size_bytes
```

The proxy also computes a full-file SHA-256. The brain uses the hash to avoid
duplicate semantic version rows when a file is re-observed unchanged. Git commit
capture is not part of the current observation path.

## State placement

Research records and pinned blobs live in the brain's selected stores:

- local brain: SQLite plus a local blob directory under its configured state
  root;
- hosted brain: Postgres plus configured S3-compatible blob storage.

The brain database is not stored in the research checkout. The checkout contains
the actual project and experiment files. The proxy separately keeps the
machine-local checkout-to-project link database in `project_links.sqlite`.

## MCP operations

- `resource.register(path=...)` observes one file.
- `resource.register(paths=[...])` observes a batch.
- Supplying `target_type`, `target_id`, and `role` associates each observed
  version in the same call; the trio is all-or-none.
- `resource.register(resource_id=..., target_type=..., target_id=..., role=...)`
  associates an already registered resource.
- `resource.find(resource_id=..., include_history=true)` resolves one resource
  and optionally its version history.
- `resource.find(...)` without `resource_id` lists filtered resources.
- `resource.delete` is an internal/UI operation hidden from agent `tools/list`.

Project scope remains explicit in brain and HTTP calls. In a project-local MCP
session the proxy injects the linked `project_id` and hides it from schemas where
the caller should not choose it.

## Rules

- Paths are repo-relative and must remain inside the configured checkout.
- Directories are not resources.
- `.research_plugin/` state cannot be registered as research evidence.
- Ignored files are not discovered automatically, but an explicit valid path may
  be registered.
- Moving and registering a file at a new path creates or revives the resource at
  that path; the proxy does not automatically rewrite the old resource.
- Deleting or moving a live file does not update brain state automatically. The
  current proxy performs explicit observations, not background reconciliation.
- Deleting a resource removes it from active tracking and associations while
  retaining version metadata for audit.
- The brain never scans a checkout and never treats an unregistered edit as a
  state mutation.
