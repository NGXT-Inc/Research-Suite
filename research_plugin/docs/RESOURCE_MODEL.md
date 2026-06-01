# Resource Model

## Goal

Replace the old resource/artifact subsystem with the smallest model that still
supports research memory:

> A resource is a regular file in the local repo.

One file maps to one resource. The backend does not maintain artifact refs or
generated manifests. It keeps append-only resource version rows with file
metadata (size, mtime, content sha256, mimetype) — but it does not store the
file contents themselves. Historical content lives in the user's own repo
(working tree or their git history).

## Mental model

Codex is free to work in the local repo. It can create, edit, delete, and inspect
ordinary files as part of normal development and experimentation.

Those files are not research resources yet.

A file becomes a research resource only when Codex runs sync/register through
the MCP server and the server accepts it. At that point, MCP stores:

- which repo-relative file path is the resource
- what file version was observed at sync time
- which experiment, claim, run, or review the file is associated with
- what role the file played, such as plan, input, code, config, result, note, or
  model
- for experiment associations, which attempt the file belongs to

The server owns the research memory that says "this file, at this observed
version, mattered for this research object." It does not own the file contents
themselves.

On a later turn, Codex asks MCP for project or experiment state, receives the
resource path, and reads the live file directly from the local repo. If the
user needs the historical content of an overwritten file, they retrieve it
from their own repo's git history.

## Core shape

```json
{
  "resource_id": "res_...",
  "project_id": "proj_...",
  "path": "experiments/eval_001/results.json",
  "kind": "dataset | result | note | code | config | model | other",
  "title": "Optional human title",
  "associations": [
    {
	      "target_type": "experiment | claim | review",
	      "target_id": "exp_...",
	      "role": "plan | input | code | config | result | note | model",
	      "attempt_index": 2,
	      "version_id": "rver_..."
	    }
	  ],
  "created_by": "codex | user",
  "last_observed": {
    "mtime_ns": 1789520738123456789,
    "size_bytes": 42183,
    "observed_at": "2026-05-17T14:21:03Z",
	    "git_commit": "optional-if-clean-and-tracked"
	  }
	}
	```

The resource identity is the repo-relative path. The observed version token is
latest-file metadata. The durable historical identity is `resource_versions.id`,
which is attached to resource associations and review fingerprints.

## State layout

The backend keeps its state under the project root:

```text
.research_plugin/
  state.sqlite
  activity.jsonl
```

SQLite is the workflow/index store:

- projects, claims, experiments, reviews, sandboxes
- resource ids and current live path
- resource version metadata (sha256, size, mtime, mimetype)
- which version was associated to which attempt/role

There is no content store — historical file content is the user's
responsibility (live working tree or their own git history).

## About using edit time

Using edit date/time alone is simple, but weak as a version identity:

- some filesystems have coarse timestamp resolution
- copies can preserve timestamps
- tools can rewrite content without meaningful timestamp semantics
- timestamp changes do not always mean semantic changes

For the lean MVP, use this stable compromise:

```text
version_token = path + mtime_ns + size_bytes
```

If the file is tracked by git and the worktree is clean enough to identify it,
also store:

```text
git_commit + path
```

MCP computes a full-file `content_sha256` so it can avoid creating duplicate
version rows when a file is re-observed without semantic change.

## Operations

The MCP server should expose resource operations that are intentionally boring:

- `resource.register_file(project_id, path, kind, title?)`
- `resource.observe_file(project_id, path)`
- `resource.sync_changed_files(project_id, paths?)`
- `resource.associate(project_id, resource_id, target_type, target_id, role)`
- `resource.list(project_id, filters?)`
- `resource.resolve(project_id, resource_id)`
- `resource.history(project_id, resource_id)`
- `resource.mark_role(project_id, resource_id, role)`

Every resource operation is project-scoped. The server must reject missing
`project_id` rather than guessing an active project.

Allowed association roles are `plan`, `input`, `code`, `config`, `result`,
`note`, `model`, and `other`. Experiment output files use the singular role
`result`; the MCP tool schema and validation errors expose this vocabulary so
agents do not need to guess.

No `artifact_ref.create`, `resource_version.create`, `manifest.create`,
`cache_resource`, `verify_artifact`, or `restore` tool in the MVP. Restoring old
content should happen as a normal live file edit followed by a new sync, which
preserves append-only history.

## Rules

- paths must be repo-relative
- paths must stay inside the configured repo root
- ignored files are not resources unless explicitly registered
- directories are not resources in v0.1
- one path maps to at most one active resource
- moving a file is a resource path update, not a new artifact lineage system
- deleting a file does not delete the resource memory; it marks the resource
  missing until restored or archived
- local files may exist without being resources
- resources may point to missing local files, but MCP must report them as missing
  in status responses
- experiment resource associations are attempt-scoped; old resources stay
  visible as history but do not satisfy gates for the current attempt
- `.research_plugin/` is backend state and cannot be registered as a resource
