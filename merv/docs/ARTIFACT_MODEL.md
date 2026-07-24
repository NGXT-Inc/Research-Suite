# Artifact Model

## Definition

> An artifact is a typed object submitted against a workflow target
> (experiment, reflection, claim, review, or attempt). The agent writes a file
> locally, calls `artifact.submit`, and uploads the bytes with the returned
> one-line command. The one system-authored exception is the brain-created
> metrics exhibit, pinned by the backend at `submit_results`.

There is no file tracking: no path identity, no version table, no observation
fingerprints. `path` survives only as a trust-based provenance label — the
relative path of the local file the agent wrote — used for display, never as
identity.

## Mandated vocabulary

Roles are exactly what the backend consumes:

- **Gated docs** (16 KB cap each): `plan`, `report`, `graph`, `project_graph`,
  `reflection_lens_doc`, `reflection_doc`, `change_spec`. Workflow gates and
  reviewers lint the pinned submission, never a later working-tree edit.
- **`result`** (16 KB cap): small metrics JSON the system exhibit ingests. The
  bytes are always pinned; JSON parsing is try-based (the path label is a
  hint, not a gate).
- **`exhibit`** (system-only): the metrics exhibit the brain pins at
  `submit_results`. Agents cannot create or replace it.

Legality (role x target type) is enforced by the artifacts association policy;
attempt scoping is unchanged: an experiment artifact belongs to the attempt
current at submit time, and older attempts' artifacts cannot satisfy the
current attempt's gates. `reflection_lens_doc` submissions require an explicit
`lens_id`; lens coverage matches on that field.

## Submit flow

1. Write the document to a local file.
2. Call `artifact.submit {project_id, target_type, target_id, role, path,
   lens_id?, title?}`. The brain validates legality and workflow-state guards BEFORE any
   bytes move, creates a `pending` artifact row with a one-time upload token
   (TTL ~15 min), and returns `{artifact_id, run}` where `run` is a
   ready-to-run line: `curl -sf -T <path> '<base>/api/artifacts/u/<token>'`.
3. Run that line verbatim. The PUT enforces the role byte cap, computes the
   sha256 server-side (the blob-store key — not tracking), pins the bytes,
   flips the row to `complete`, and supersedes any previous complete artifact
   in the same slot.
4. For gated markdown with relative image links, the PUT response returns one
   follow-up `run` line per figure (one-time figure tokens); run each the same
   way. Figures are capped at 5 MB.

Slot identity is `(project, target, role, attempt, lens_id, path)`. A resubmit
mints a NEW artifact id and deletes the old row, so review snapshot ids
(`artifact_id:role:attempt`) invalidate naturally. Pending rows expire and are
swept on access.

The upload PUT routes are token-bearer and bypass the hosted auth gate — the
one-time token is the credential — so the bare `curl` works against both local
and hosted brains.

## Durable records

The brain stores one `artifacts` row per submission: id (`art_<hex>`), target,
role, attempt index, lens id, path label, title, sha256, size, content type,
status, and creator. `artifact_figures` holds figure rows per markdown
artifact. Bytes live in the sha256-keyed, project-namespaced blob store
(local directory or S3-compatible, per deployment).

## Reads

- `artifact.find` — resolve one artifact by id, or list a project's complete
  artifacts filtered by target/role.
- UI routes: `GET /api/projects/{pid}/artifacts`,
  `.../artifacts/{aid}/content`, `.../artifacts/{aid}/file`,
  `.../artifacts/{aid}/figure?rel=`.

## Rules

- The brain never reads a checkout; every consumed byte arrives via upload.
- Changing a local file changes nothing in the brain — fix the file and submit
  again.
- Arbitrary untyped registration (the old `code`/`config`/`input`/`note`/
  `model`/`other` kinds) no longer exists; heavy or bulky files belong in
  object storage (see STORAGE_MODEL.md).
- Rows backfilled from the pre-cut resource system may carry legacy role
  spellings (`reflection`, `synthesis_doc`, `proposals`, reflection-target
  `graph`); they are readable but rejected at submit with the replacement
  named.
