# Backend restructure — move map + tranche playbook

> **Transient document.** Guides the tranche-by-tranche physical restructure
> that makes the backend tree match the module map enforced by
> `tests/structure/test_module_boundaries.py`. The final tranche deletes this
> file. The module map itself lives in `docs/MODULE_BOUNDARIES.md`.
>
> Legacy paths below are deliberate history: they document the move itself and
> are exempt from the zero-legacy sweep.

## Status

- **Phase 1 — executed.** Five parallel tranches (feed, object_storage,
  kernel, sandbox, research_core) moved every module behind
  identity-preserving shims; merged on `restructure` (a2eb9c4, suite
  1250/34/0).
- **Phase 2 — executed** (branch `restructure-final`). The tree is re-rooted
  at the ratified `src/merv/` namespace: `backend/` → `src/merv/brain/`,
  `mcp_server/` → `src/merv/proxy/`, `research_plugin_shared/` →
  `src/merv/shared/`. All 43 shims plus the vacated legacy package inits are
  deleted; every consumer (imports, patch targets, lazy dotted strings,
  scripts, docs, sweeps) speaks final `merv.brain.*` / `merv.proxy` /
  `merv.shared` paths; the legacy classifier entries/prefixes are dropped;
  the multi-root test sweeps are collapsed to end-state scopes; the
  `kernel/env.py` logger is back on `__name__`; packaging targets `src/`
  (packages.find where=src, console scripts, proxy package-data, src-pathed
  bin wrappers and Dockerfile).
- **Remaining:**
  1. **Surface tranche** — fold the surface strays (`tools/`, `transport/`,
     `composition/`, `control/`, `services/`, `config.py`, `client_cli.py`,
     `workspace.py`, `observability.py`) into `brain/surface/`, and execute
     the `dataplane/` → proxy move per the map below.
  2. **Boundary decision** — ratify (or reject) the dataplane-ownership edge
     the surface tranche implies before moving it.
  3. **Final doc sweep** — refresh `MODULE_BOUNDARIES.md` /
     `CONTROL_DATA_PLANE_SPLIT.md` physical-path tables to the end state,
     sweep stale test method names, then delete this file and
     `RESTRUCTURE_DESHIM_INVENTORY.md`.

## Tranche playbook

The transition runs as a TWO-PHASE model executed by PARALLEL tranches.

**Phase 1 — canonical move + classified compatibility shims (one tranche per
module, all branched from the SAME base, run concurrently).** A tranche is a
PURE MOVE of its own module's files only: `git mv` + relative-depth fixes
INSIDE moved files + an identity-preserving shim at EVERY old path + line-
scoped classifier/sweep updates. Filenames keep their exact names; no logic
edits, no drive-by cleanups; consumers are NOT rewritten (the shims keep every
old import, patch target, and lazy dotted string working unchanged). The full
suite must finish with counts byte-identical to the shared baseline
(1250 passed, 34 skipped, 0 failed at base 755d3e5).

**Shim pattern.** File shim: `import sys` + import the canonical module +
`sys.modules[__name__] = _moved`. Package shim: the same, PLUS old dotted
submodule paths must resolve to the *canonical* module objects — bare aliasing
is NOT sufficient (loading `old_pkg.sub` through the aliased `__path__`
re-executes the file as a second module object). The ratified mechanism is the
sandbox tranche's lazy meta-path finder (an `(old_prefix, new_prefix)`
resolver installed by the package shim; lazy, identity-preserving in both
import orders, patch-safe under both spellings). Eager submodule
pre-registration is an acceptable variant only for small, cheap-to-import
packages. Every shim carries a "deleted at de-shim" marker comment. Shims KEEP
their legacy classifier entries until de-shim (shim→canonical is a same-module
edge, always legal).

**Phase 2 — integration/de-shim (one coordinated pass after all tranches
merge).** Rewrite every consumer to canonical paths using the per-tranche
review inventories, delete all shims, drop the legacy classifier
entries/prefixes, collapse multi-root test sweeps to end-state scopes, and
restore any pinned names the shims preserved (e.g. the `backend.env` logger
pin reverts to `__name__` together with its assertLogs tests).

**Per-tranche mechanics.** (1) Branch `restructure-tN-<module>` from the
shared base — parallel tranches do NOT wait for each other; disjoint file
ownership is the law (own module's files + line-scoped classifier/sweep edits
only; do not reformat shared tables). (2) Old paths that belong to sibling
modules stay old (their shims cover them) — only recompute relative dot-depth
against the new location; intra-module imports flip to `.` siblings.
(3) Bare dotted strings — `mock.patch("backend....")` targets and the
`_import_attr("backend....")` lazy imports in `mcp_server/local_data_plane.py`
— never show up in an `import`-shaped grep; with shims they keep working, but
they belong in the tranche's reported de-shim inventory. (4) Append the move
commit's sha to `/.git-blame-ignore-revs` (the orchestrator consolidates
shas from parallel branches). (5) Commit messages end with the standard
`Co-Authored-By: Claude Fable 5` trailer.

**Verification bar.** Per tranche: `cd merv && python3 -m pytest -q` with
byte-identical counts, PLUS identity smoke checks — for every shimmed module
(including dotted submodules) assert old-name `is` new-name in BOTH import
orders. Suite counts alone cannot detect a duplicated module object.
`mcp_server/_tool_catalog.json` must be byte-untouched. At integration
additionally: build a wheel, install into a clean environment, import every
entry point, and run the suite from the installed package — source-checkout
tests have previously missed packaging failures.

**Ratchet mixed-state pattern.** `FILE_MODULES` (file-exact, wins) +
`PACKAGE_MODULES` (deepest-prefix wins) classify every backend file exactly
once. As each module physically moves, its file-exact entries are DELETED and
replaced by ONE package-prefix entry (`"feed" → FEED`); unmoved modules keep
their file-exact entries. The classifier already resolves longest-prefix-first
and needs no changes. Two subtests enforce the flip mechanically:
`test_every_backend_file_is_classified` fails if the new location is not
covered, and `test_classification_tables_carry_no_stale_paths` fails if a
moved file's old entry — or a prefix for an emptied/moved directory — is left
behind (e.g. drop `"services/sandbox"` when the sandbox tranche lands, drop
`"dataplane"` when it leaves backend). `backend/__init__.py` stays at the
package root forever, so its `FILE_MODULES` kernel entry survives to the end
state. The module's `ALLOWED_EDGES` must not change — the law is ratified,
only paths move.

**Sweep coverage rule.** Several structure tests enumerate the LEGACY
packages (`SERVICES_ROOT.rglob`, `DOMAIN_ROOT.glob`, the `CONTROL_MODULES`
list in `test_plane_layout.py`, expected import-name sets in
`test_service_layout.py`). Files leaving those packages silently leave those
nets. Per moved file, extend a sweep to the new module root iff its property
is still demanded of that file (plane placement / control-servability, store
discipline); do NOT extend consumer-specific lints (e.g. the sandbox-backend
contract sweeps) to modules that never touch that contract. The final tranche
collapses these multi-root sweeps to their end-state scopes.

**Final tranche additionally:** delete this file, and run the FULL-TREE
sweep: `git grep` EVERY tracked file (not just backend/mcp_server/tests/
scripts — also pyproject.toml and packaging metadata, deploy/, bin/, clients/,
agents/, skills/, docs/, .mcp*.json and other manifests, research_state_ui
comments) for every old path in BOTH dotted (`backend.services.feed`) and
slash (`backend/services/feed`) forms; zero hits outside deliberate history
notes. Sweep stale test method names (e.g.
`test_feed_policy_is_domain_leaf_module` — the "domain" is historical).

## Move map

File-exact move map generated from `FILE_MODULES`/`PACKAGE_MODULES`. Paths
are `merv/backend`-relative unless prefixed `merv/`.

### kernel — 22 files, 21 move
- `backend/__init__.py` → *(stays)*
- `backend/env.py` → `backend/kernel/env.py`
- `backend/ports/__init__.py` → `backend/kernel/ports/__init__.py`
- `backend/ports/mgmt_keys.py` → `backend/kernel/ports/mgmt_keys.py`
- `backend/ports/object_store.py` → `backend/kernel/ports/object_store.py`
- `backend/ports/quota_admission.py` → `backend/kernel/ports/quota_admission.py`
- `backend/ports/reflection_writers.py` → `backend/kernel/ports/reflection_writers.py`
- `backend/ports/resource_records.py` → `backend/kernel/ports/resource_records.py`
- `backend/ports/review_policy.py` → `backend/kernel/ports/review_policy.py`
- `backend/ports/sandbox_lifecycle.py` → `backend/kernel/ports/sandbox_lifecycle.py`
- `backend/ports/sandbox_worker.py` → `backend/kernel/ports/sandbox_worker.py`
- `backend/ports/task_channel.py` → `backend/kernel/ports/task_channel.py`
- `backend/ports/workflow_readers.py` → `backend/kernel/ports/workflow_readers.py`
- `backend/secret_tokens.py` → `backend/kernel/secret_tokens.py`
- `backend/state/__init__.py` → `backend/kernel/state/__init__.py`
- `backend/state/activity.py` → `backend/kernel/state/activity.py`
- `backend/state/dialects.py` → `backend/kernel/state/dialects.py`
- `backend/state/store.py` → `backend/kernel/state/store.py`
- `backend/state/tool_call_stats.py` → `backend/kernel/state/tool_call_stats.py`
- `backend/state/tool_calls.py` → `backend/kernel/state/tool_calls.py`
- `backend/utils.py` → `backend/kernel/utils.py`
- `backend/version.py` → `backend/kernel/version.py`

### research_core — 30 files, 30 move
- `backend/domain/__init__.py` → `backend/research_core/domain/__init__.py`
- `backend/domain/artifacts.py` → `backend/research_core/domain/artifacts.py`
- `backend/domain/experiment_names.py` → `backend/research_core/domain/experiment_names.py`
- `backend/domain/experiment_policy.py` → `backend/research_core/domain/experiment_policy.py`
- `backend/domain/gates.py` → `backend/research_core/domain/gates.py`
- `backend/domain/graph_lint.py` → `backend/research_core/domain/graph_lint.py`
- `backend/domain/paths.py` → `backend/research_core/domain/paths.py`
- `backend/domain/reflection_artifacts.py` → `backend/research_core/domain/reflection_artifacts.py`
- `backend/domain/reflection_gates.py` → `backend/research_core/domain/reflection_gates.py`
- `backend/domain/reflection_policy.py` → `backend/research_core/domain/reflection_policy.py`
- `backend/domain/review_gates.py` → `backend/research_core/domain/review_gates.py`
- `backend/domain/review_handoff.py` → `backend/research_core/domain/review_handoff.py`
- `backend/domain/review_returns.py` → `backend/research_core/domain/review_returns.py`
- `backend/domain/review_snapshot.py` → `backend/research_core/domain/review_snapshot.py`
- `backend/domain/synopsis.py` → `backend/research_core/domain/synopsis.py`
- `backend/domain/vocabulary.py` → `backend/research_core/domain/vocabulary.py`
- `backend/domain/workflow_gates.py` → `backend/research_core/domain/workflow_gates.py`
- `backend/services/association_targets.py` → `backend/research_core/association_targets.py`
- `backend/services/claims.py` → `backend/research_core/claims.py`
- `backend/services/experiment_views.py` → `backend/research_core/experiment_views.py`
- `backend/services/experiments.py` → `backend/research_core/experiments.py`
- `backend/services/graph_refs.py` → `backend/research_core/graph_refs.py`
- `backend/services/project_overview.py` → `backend/research_core/project_overview.py`
- `backend/services/projects.py` → `backend/research_core/projects.py`
- `backend/services/reflection_tools.py` → `backend/research_core/reflection_tools.py`
- `backend/services/reflections.py` → `backend/research_core/reflections.py`
- `backend/services/review_gate.py` → `backend/research_core/review_gate.py`
- `backend/services/reviews.py` → `backend/research_core/reviews.py`
- `backend/services/workflow.py` → `backend/research_core/workflow.py`
- `backend/services/workflow_views.py` → `backend/research_core/workflow_views.py`

### artifacts — 7 files, 0 move
- `backend/artifacts/__init__.py` → *(stays)*
- `backend/artifacts/figure_view.py` → *(stays)*
- `backend/artifacts/markdown_images.py` → *(stays)*
- `backend/artifacts/pinned.py` → *(stays)*
- `backend/artifacts/resource_selection.py` → *(stays)*
- `backend/artifacts/resources.py` → *(stays)*
- `backend/artifacts/roles.py` → *(stays)*

### object_storage — 7 files, 7 move
- `backend/domain/storage_guidance.py` → `backend/object_storage/storage_guidance.py`
- `backend/storage/__init__.py` → `backend/object_storage/__init__.py`
- `backend/storage/blobs.py` → `backend/object_storage/blobs.py`
- `backend/storage/file_transfer.py` → `backend/object_storage/file_transfer.py`
- `backend/storage/s3_blobs.py` → `backend/object_storage/s3_blobs.py`
- `backend/storage/s3_object_store.py` → `backend/object_storage/s3_object_store.py`
- `backend/storage/service.py` → `backend/object_storage/service.py`

### sandbox — 70 files, 65 move
- `backend/domain/sandbox_paths.py` → `backend/sandbox/sandbox_paths.py`
- `backend/execution/__init__.py` → `backend/sandbox/execution/__init__.py`
- `backend/execution/backends/__init__.py` → `backend/sandbox/execution/backends/__init__.py`
- `backend/execution/backends/digitalocean/__init__.py` → `backend/sandbox/execution/backends/digitalocean/__init__.py`
- `backend/execution/backends/digitalocean/catalog.py` → `backend/sandbox/execution/backends/digitalocean/catalog.py`
- `backend/execution/backends/digitalocean/client.py` → `backend/sandbox/execution/backends/digitalocean/client.py`
- `backend/execution/backends/digitalocean/config.py` → `backend/sandbox/execution/backends/digitalocean/config.py`
- `backend/execution/backends/digitalocean/sandbox_backend.py` → `backend/sandbox/execution/backends/digitalocean/sandbox_backend.py`
- `backend/execution/backends/fake/__init__.py` → `backend/sandbox/execution/backends/fake/__init__.py`
- `backend/execution/backends/hyperstack/__init__.py` → `backend/sandbox/execution/backends/hyperstack/__init__.py`
- `backend/execution/backends/hyperstack/catalog.py` → `backend/sandbox/execution/backends/hyperstack/catalog.py`
- `backend/execution/backends/hyperstack/client.py` → `backend/sandbox/execution/backends/hyperstack/client.py`
- `backend/execution/backends/hyperstack/config.py` → `backend/sandbox/execution/backends/hyperstack/config.py`
- `backend/execution/backends/hyperstack/sandbox_backend.py` → `backend/sandbox/execution/backends/hyperstack/sandbox_backend.py`
- `backend/execution/backends/lambda_labs/__init__.py` → `backend/sandbox/execution/backends/lambda_labs/__init__.py`
- `backend/execution/backends/lambda_labs/catalog.py` → `backend/sandbox/execution/backends/lambda_labs/catalog.py`
- `backend/execution/backends/lambda_labs/client.py` → `backend/sandbox/execution/backends/lambda_labs/client.py`
- `backend/execution/backends/lambda_labs/config.py` → `backend/sandbox/execution/backends/lambda_labs/config.py`
- `backend/execution/backends/lambda_labs/sandbox_backend.py` → `backend/sandbox/execution/backends/lambda_labs/sandbox_backend.py`
- `backend/execution/backends/modal/__init__.py` → `backend/sandbox/execution/backends/modal/__init__.py`
- `backend/execution/backends/modal/_sandbox_ops.py` → `backend/sandbox/execution/backends/modal/_sandbox_ops.py`
- `backend/execution/backends/modal/config.py` → `backend/sandbox/execution/backends/modal/config.py`
- `backend/execution/backends/modal/sandbox_backend.py` → `backend/sandbox/execution/backends/modal/sandbox_backend.py`
- `backend/execution/backends/tensordock/__init__.py` → `backend/sandbox/execution/backends/tensordock/__init__.py`
- `backend/execution/backends/tensordock/catalog.py` → `backend/sandbox/execution/backends/tensordock/catalog.py`
- `backend/execution/backends/tensordock/client.py` → `backend/sandbox/execution/backends/tensordock/client.py`
- `backend/execution/backends/tensordock/config.py` → `backend/sandbox/execution/backends/tensordock/config.py`
- `backend/execution/backends/tensordock/sandbox_backend.py` → `backend/sandbox/execution/backends/tensordock/sandbox_backend.py`
- `backend/execution/backends/thunder_compute/__init__.py` → `backend/sandbox/execution/backends/thunder_compute/__init__.py`
- `backend/execution/backends/thunder_compute/catalog.py` → `backend/sandbox/execution/backends/thunder_compute/catalog.py`
- `backend/execution/backends/thunder_compute/client.py` → `backend/sandbox/execution/backends/thunder_compute/client.py`
- `backend/execution/backends/thunder_compute/config.py` → `backend/sandbox/execution/backends/thunder_compute/config.py`
- `backend/execution/backends/thunder_compute/sandbox_backend.py` → `backend/sandbox/execution/backends/thunder_compute/sandbox_backend.py`
- `backend/execution/backends/verda/__init__.py` → `backend/sandbox/execution/backends/verda/__init__.py`
- `backend/execution/backends/verda/catalog.py` → `backend/sandbox/execution/backends/verda/catalog.py`
- `backend/execution/backends/verda/client.py` → `backend/sandbox/execution/backends/verda/client.py`
- `backend/execution/backends/verda/config.py` → `backend/sandbox/execution/backends/verda/config.py`
- `backend/execution/backends/verda/sandbox_backend.py` → `backend/sandbox/execution/backends/verda/sandbox_backend.py`
- `backend/execution/backends/vm_ssh_backend.py` → `backend/sandbox/execution/backends/vm_ssh_backend.py`
- `backend/execution/backends/voltage_park/__init__.py` → `backend/sandbox/execution/backends/voltage_park/__init__.py`
- `backend/execution/backends/voltage_park/catalog.py` → `backend/sandbox/execution/backends/voltage_park/catalog.py`
- `backend/execution/backends/voltage_park/client.py` → `backend/sandbox/execution/backends/voltage_park/client.py`
- `backend/execution/backends/voltage_park/config.py` → `backend/sandbox/execution/backends/voltage_park/config.py`
- `backend/execution/backends/voltage_park/sandbox_backend.py` → `backend/sandbox/execution/backends/voltage_park/sandbox_backend.py`
- `backend/execution/bootstrap_tools.py` → `backend/sandbox/execution/bootstrap_tools.py`
- `backend/execution/multiplexer.py` → `backend/sandbox/execution/multiplexer.py`
- `backend/execution/run_receipts.py` → `backend/sandbox/execution/run_receipts.py`
- `backend/execution/sync_dirs.py` → `backend/sandbox/execution/sync_dirs.py`
- `backend/execution/transcript_wire.py` → `backend/sandbox/execution/transcript_wire.py`
- `backend/execution/usage_metrics.py` → `backend/sandbox/execution/usage_metrics.py`
- `backend/execution/vm_bootstrap.py` → `backend/sandbox/execution/vm_bootstrap.py`
- `backend/execution/vm_ssh.py` → `backend/sandbox/execution/vm_ssh.py`
- `backend/sandbox/__init__.py` → *(stays)*
- `backend/sandbox/managed_mgmt_keys.py` → *(stays)*
- `backend/sandbox/mgmt_keys.py` → *(stays)*
- `backend/sandbox/sandbox_backend.py` → *(stays)*
- `backend/sandbox/sandbox_support.py` → *(stays)*
- `backend/services/quotas.py` → `backend/sandbox/quotas.py`
- `backend/services/sandbox/__init__.py` → *(becomes the transitional package shim; deleted at de-shim — `backend/sandbox/__init__.py` already exists)*
- `backend/services/sandbox/sandbox_daemons.py` → `backend/sandbox/sandbox_daemons.py`
- `backend/services/sandbox/sandbox_heartbeat.py` → `backend/sandbox/sandbox_heartbeat.py`
- `backend/services/sandbox/sandbox_lifecycle.py` → `backend/sandbox/sandbox_lifecycle.py`
- `backend/services/sandbox/sandbox_metrics.py` → `backend/sandbox/sandbox_metrics.py`
- `backend/services/sandbox/sandbox_provisioner.py` → `backend/sandbox/sandbox_provisioner.py`
- `backend/services/sandbox/sandbox_registry.py` → `backend/sandbox/sandbox_registry.py`
- `backend/services/sandbox/sandbox_runs.py` → `backend/sandbox/sandbox_runs.py`
- `backend/services/sandbox/sandbox_views.py` → `backend/sandbox/sandbox_views.py`
- `backend/services/sandbox/sandboxes.py` → `backend/sandbox/sandboxes.py`
- `backend/services/transcript_cache.py` → `backend/sandbox/transcript_cache.py`
- `backend/ssh_keys.py` → `backend/sandbox/ssh_keys.py`

### feed — 5 files, 5 move *(executed in T1)*
- `backend/domain/feed_embeds.py` → `backend/feed/feed_embeds.py`
- `backend/domain/feed_images.py` → `backend/feed/feed_images.py`
- `backend/domain/feed_policy.py` → `backend/feed/feed_policy.py`
- `backend/services/feed.py` → `backend/feed/feed.py`
- `backend/services/feed_unfurl.py` → `backend/feed/feed_unfurl.py`

### mlflow — 6 files, 0 move
- `backend/mlflow/__init__.py` → *(stays)*
- `backend/mlflow/config.py` → *(stays)*
- `backend/mlflow/exhibit.py` → *(stays)*
- `backend/mlflow/local_server.py` → *(stays)*
- `backend/mlflow/metrics.py` → *(stays)*
- `backend/mlflow/tracking.py` → *(stays)*

### surface — 57 files, 57 move
- `backend/client_cli.py` → `backend/surface/client_cli.py`
- `backend/composition/__init__.py` → `backend/surface/composition/__init__.py`
- `backend/composition/brain_dirs.py` → `backend/surface/composition/brain_dirs.py`
- `backend/composition/control_mode.py` → `backend/surface/composition/control_mode.py`
- `backend/config.py` → `backend/surface/config.py`
- `backend/control/__init__.py` → `backend/surface/control/__init__.py`
- `backend/control/control_app.py` → `backend/surface/control/control_app.py`
- `backend/control/control_client.py` → `backend/surface/control/control_client.py`
- `backend/control/control_runtime.py` → `backend/surface/control/control_runtime.py`
- `backend/control/record_core.py` → `backend/surface/control/record_core.py`
- `backend/dataplane/__init__.py` → `merv/mcp_server/dataplane/__init__.py`
- `backend/dataplane/experiment_folders.py` → `merv/mcp_server/dataplane/experiment_folders.py`
- `backend/dataplane/feed_embeds.py` → `merv/mcp_server/dataplane/feed_embeds.py`
- `backend/dataplane/feed_images.py` → `merv/mcp_server/dataplane/feed_images.py`
- `backend/dataplane/repo_paths.py` → `merv/mcp_server/dataplane/repo_paths.py`
- `backend/dataplane/resource_artifacts.py` → `merv/mcp_server/dataplane/resource_artifacts.py`
- `backend/dataplane/resource_observer.py` → `merv/mcp_server/dataplane/resource_observer.py`
- `backend/dataplane/resource_validation.py` → `merv/mcp_server/dataplane/resource_validation.py`
- `backend/dataplane/sandbox_outputs.py` → `merv/mcp_server/dataplane/sandbox_outputs.py`
- `backend/observability.py` → `backend/surface/observability.py`
- `backend/services/__init__.py` → `backend/surface/__init__.py`
- `backend/services/auth.py` → `backend/surface/auth.py`
- `backend/services/cleanup.py` → `backend/surface/cleanup.py`
- `backend/services/identity.py` → `backend/surface/identity.py`
- `backend/services/permissions.py` → `backend/surface/permissions.py`
- `backend/tools/__init__.py` → `backend/surface/tools/__init__.py`
- `backend/tools/contracts.py` → `backend/surface/tools/contracts.py`
- `backend/tools/exhibits.py` → `backend/surface/tools/exhibits.py`
- `backend/tools/feed_contracts.py` → `backend/surface/tools/feed_contracts.py`
- `backend/tools/tool_facade.py` → `backend/surface/tools/tool_facade.py`
- `backend/tools/tool_handlers.py` → `backend/surface/tools/tool_handlers.py`
- `backend/transport/__init__.py` → `backend/surface/transport/__init__.py`
- `backend/transport/admin_http.py` → `backend/surface/transport/admin_http.py`
- `backend/transport/api/__init__.py` → `backend/surface/transport/api/__init__.py`
- `backend/transport/api/app.py` → `backend/surface/transport/api/app.py`
- `backend/transport/api/claims.py` → `backend/surface/transport/api/claims.py`
- `backend/transport/api/context.py` → `backend/surface/transport/api/context.py`
- `backend/transport/api/events.py` → `backend/surface/transport/api/events.py`
- `backend/transport/api/experiments.py` → `backend/surface/transport/api/experiments.py`
- `backend/transport/api/feed.py` → `backend/surface/transport/api/feed.py`
- `backend/transport/api/meta.py` → `backend/surface/transport/api/meta.py`
- `backend/transport/api/projects.py` → `backend/surface/transport/api/projects.py`
- `backend/transport/api/reflections.py` → `backend/surface/transport/api/reflections.py`
- `backend/transport/api/resources.py` → `backend/surface/transport/api/resources.py`
- `backend/transport/api/reviews.py` → `backend/surface/transport/api/reviews.py`
- `backend/transport/api/sandboxes.py` → `backend/surface/transport/api/sandboxes.py`
- `backend/transport/api/sdk_auth.py` → `backend/surface/transport/api/sdk_auth.py`
- `backend/transport/api/shared.py` → `backend/surface/transport/api/shared.py`
- `backend/transport/api/storage.py` → `backend/surface/transport/api/storage.py`
- `backend/transport/api/views.py` → `backend/surface/transport/api/views.py`
- `backend/transport/data_plane_http.py` → `backend/surface/transport/data_plane_http.py`
- `backend/transport/feed_http.py` → `backend/surface/transport/feed_http.py`
- `backend/transport/http_api.py` → `backend/surface/transport/http_api.py`
- `backend/transport/http_policy.py` → `backend/surface/transport/http_policy.py`
- `backend/transport/http_server.py` → `backend/surface/transport/http_server.py`
- `backend/transport/mcp_http.py` → `backend/surface/transport/mcp_http.py`
- `backend/workspace.py` → `backend/surface/workspace.py`
