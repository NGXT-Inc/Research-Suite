# Module Boundaries

Target shape: a modular monolith — one kernel, five modules plus the MLflow
extension, and a surface that composes them.

```
                    ┌───────────────────────── SURFACE ─────────────────────────┐
                    │  tools/ transport/ composition/ control/ daemon/          │
                    │  dataplane/ app config client_cli  (imports anything)     │
                    └───────┬──────────┬──────────┬─────────┬─────────┬─────────┘
                            ▼          ▼          ▼         ▼         ▼
   MLFLOW ──────▶ RESEARCH_CORE   ARTIFACTS   OBJECT_   SANDBOX     FEED
 (extension)          │    │          │       STORAGE                │
                      │    └─────────▶│          ▲                   │
                      │   (allowance) └─────────▶│◀──────────────────┘
                      ▼                    (allowances)
                    KERNEL   (db/transactions/events/ids — imports only itself)
```

## Import law

- kernel imports only kernel.
- Each module imports only itself + kernel, plus these ratified allowances:
  - `research_core -> artifacts`: workflow gates judge pinned artifact bytes.
  - `artifacts -> object_storage` and `feed -> object_storage`: resource
    versions and feed images persist their bytes through the blob stores.
  - `mlflow -> research_core`: the extension reads experiment records.
- surface imports anything. **Nothing imports surface.**

## Module → package mapping

| Module         | Backend code                                                                |
|----------------|-----------------------------------------------------------------------------|
| kernel         | `state/*` (minus blobs), `ports/*`, `utils`, `env`, `version`, `secret_tokens` |
| research_core  | workflow/experiments/claims/reviews/syntheses/projects services + views, `graph_refs`, `reflection_tools`, `domain/*` (minus overrides) |
| artifacts      | `services/{resources,pinned,figure_view}`, `domain/resource_selection`       |
| object_storage | `storage/*`, `state/{blobs,s3_blobs}`, `domain/storage_guidance`             |
| sandbox        | `services/sandbox/*`, `sandbox/*`, `execution/*`, `services/{transcript_cache,quotas}`, `domain/{sandbox_paths,quota_contract}`, `ssh_keys` |
| feed           | `services/{feed,feed_unfurl}`, `domain/{feed_images,feed_policy}`            |
| mlflow         | `mlflow/*` (extension)                                                       |
| surface        | `tools/*`, `transport/*`, `composition/*`, `control/*`, `daemon/*`, `dataplane/*`, `app`, `config`, `client_cli`, glue services (`permissions`, `identity`, `cleanup`), `local_runtime`, `workspace`, `observability` |

The authoritative, file-exact table is `FILE_MODULES`/`PACKAGE_MODULES` in
`tests/structure/test_module_boundaries.py`.

## How the ratchet works

`tests/structure/test_module_boundaries.py` AST-scans every import (top-level
and function-local) in backend production code, maps importer and imported
file to modules, and checks the edge against the law above. Today's violating
pairs are frozen in `GRANDFATHERED`. The baseline only shrinks: any new
cross-module import fails immediately, and when a grandfathered edge is fixed
the test fails until its line is deleted — so drift is impossible and every
cleanup is locked in. New backend files must be classified in the same test
before they can land.
