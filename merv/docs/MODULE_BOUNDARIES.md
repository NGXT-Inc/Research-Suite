# Module Boundaries

Implemented shape: a modular monolith — one kernel, five modules plus the
MLflow extension, and a surface that composes them.

```
                    ┌───────────────────────── SURFACE ─────────────────────────┐
                    │  tools/ transport/ composition/ control/ dataplane/       │
                    │  config client_cli  (imports anything)                    │
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
| kernel         | `state/*` (incl. `tool_call_stats`), `ports/*` (incl. the `AdmissionRequest` contract in `ports/quota_admission`), `utils`, `env`, `version`, `secret_tokens` |
| research_core  | workflow/experiments/claims/reviews/reflections/projects services + views, `graph_refs`, `reflection_tools`, `domain/*` (minus overrides) |
| artifacts      | `artifacts/*` (resources, pinned + PinnedStore facade, roles, markdown_images, figure_view, resource_selection) |
| object_storage | `object_storage/*` (transitional shims stay at `storage/*` and `domain/storage_guidance` until de-shim) |
| sandbox        | `services/sandbox/*`, `sandbox/*` (incl. the `mgmt_keys`/`managed_mgmt_keys` custody adapters), `execution/*`, `services/{transcript_cache,quotas}`, `domain/sandbox_paths`, `ssh_keys` |
| feed           | `services/{feed,feed_unfurl}`, `domain/{feed_images,feed_embeds,feed_policy}` |
| mlflow         | `mlflow/*` (extension, incl. its own env config in `mlflow/config`)          |
| surface        | `tools/*`, `transport/*`, `composition/*`, `control/*`, `dataplane/*`, `config`, `client_cli`, glue services (`permissions`, `identity`, `cleanup`), `workspace`, `observability` |

The authoritative, file-exact table is `FILE_MODULES`/`PACKAGE_MODULES` in
`tests/structure/test_module_boundaries.py`.

## How the ratchet works

`tests/structure/test_module_boundaries.py` AST-scans every import (top-level
and function-local) in backend production code, maps importer and imported
file to modules, and checks the edge against the law above. `GRANDFATHERED` is
empty: every import follows the law, and any new cross-module import fails
immediately. New backend files must be classified in the same test before they
can land.

Two module-content rules ride along with the import law:

- **SQL ownership:** module SQL may name only tables owned by that module, the
  kernel, or an allowed dependency. This is enforced by
  `test_module_sql_respects_table_ownership`; attachment ids therefore remain
  opaque inside the sandbox module, while the surface injects research-core
  existence/scope checks.
- **Provider neutrality:** sandbox services do not dispatch on provider-name
  literals. Provider differences are expressed as `BackendCapabilities` flags
  and implemented under `execution/backends/<provider>/`, enforced by
  `test_services_do_not_dispatch_on_provider_name_literals`.
