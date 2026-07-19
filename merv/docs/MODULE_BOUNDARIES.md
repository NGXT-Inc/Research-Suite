# Module Boundaries

Implemented brain shape: a modular monolith — one kernel, five modules plus the
MLflow extension, and a surface that composes them. The local proxy and pure
shared layer sit outside this brain-only module law.

```
                    ┌───────────────────────── SURFACE ─────────────────────────┐
                    │  surface/: tools/ transport/ composition/ control/        │
                    │  config observability glue  (imports anything)            │
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

Across planes, the reverse boundary is equally strict: brain code may import
pure `merv.shared` contracts but never `merv.proxy`; proxy code may import only
the standard library, `merv.proxy`, and `merv.shared`; shared code imports only
the standard library and itself, never either plane.

## Module → package mapping

| Module         | Backend code                                                                |
|----------------|-----------------------------------------------------------------------------|
| kernel         | `kernel/state/*` (incl. `tool_call_stats`), `kernel/ports/*` (incl. the `AdmissionRequest` contract in `ports/quota_admission`), `kernel/{utils,env,version,secret_tokens}` |
| research_core  | `research_core/*` (workflow/experiments/claims/reviews/reflections/projects services + views, `graph_refs`, `reflection_tools`), `research_core/domain/*` |
| artifacts      | `artifacts/*` (resources, pinned + PinnedStore facade, figure_view, resource_selection) |
| object_storage | `object_storage/*` (blob/object-store adapters and ledger service) |
| sandbox        | `sandbox/*` (incl. the `mgmt_keys`/`managed_mgmt_keys` custody adapters, `sandbox_paths`, `ssh_keys`, `transcript_cache`, `quotas`), `sandbox/execution/*` |
| feed           | `feed/*` (feed, feed_unfurl, feed_policy)                                   |
| mlflow         | `mlflow/*` (extension, incl. its own env config in `mlflow/config`)          |
| surface        | `surface/*` — one physical package: `surface/{tools,transport,composition,control}/*`, glue services (`auth`, `permissions`, `identity`, `cleanup`), `surface/config`, `surface/observability` |

Outside the brain modular-monolith classifier:

| Layer | Code |
|---|---|
| local proxy | `src/merv/proxy/*`, including `dataplane/*` and `workspace.py` |
| pure shared | `src/merv/shared/*`, including errors, path/wire/tool contracts, storage helpers, feed media, artifact roles, and markdown parsing |
| login CLI | `src/merv/client/*` — ships in the slim plugin bundle; imports only stdlib + `merv.shared`, never `merv.brain` |

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
  and implemented under `sandbox/execution/backends/<provider>/`, enforced by
  `test_services_do_not_dispatch_on_provider_name_literals`.
