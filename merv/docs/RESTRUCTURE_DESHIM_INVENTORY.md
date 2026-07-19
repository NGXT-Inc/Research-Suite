# De-shim inventories — consolidated from the five Sol tranche reviews

> Transient integration input; deleted with RESTRUCTURE_MAP.md at the end.

## feed (T1)
## Verdict: CORRECTIONS REQUIRED

The FEED move itself is clean and behavior-preserving. Corrections are required because the included transition playbook is incompatible with the parallel, identity-shimmed tranches and contains one impossible move-map entry.

### Defects

1. **High — the playbook assumes sequential physical moves, contradicting the actual parallel shim workflow.** It names T1 as the reference, limits tranches to `git mv`, requires the previous tranche to be merged first, and instructs removal of legacy classifier entries ([RESTRUCTURE_MAP.md:10](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:10), [RESTRUCTURE_MAP.md:13](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:13), [RESTRUCTURE_MAP.md:21](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:21), [RESTRUCTURE_MAP.md:46](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:46)). Identity-preserving shims necessarily leave executable files at old paths and require their classifier entries until de-shim. The runbook needs an explicit two-phase model: canonical move plus classified compatibility shims, followed by a coordinated rewrite/de-shim pass.

2. **Medium — verification is insufficient for identity shims and installed packaging.** The only general verification is the source-tree pytest run ([RESTRUCTURE_MAP.md:38](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:38)). Passing counts cannot detect two module objects loaded under old/new names, import-order asymmetry, or missing wheel packages. The repository itself warns that source-checkout tests previously missed packaging failures ([pyproject.toml:43](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/pyproject.toml:43)). Require old/new `is` identity smoke checks in both import orders—including submodules—and a built-wheel import/entry-point smoke test.

3. **Medium — the reference sweep is too narrow for de-shimming.** It searches only four `merv` subtrees ([RESTRUCTURE_MAP.md:26](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:26)); the final step mentions only docs and stale test names ([RESTRUCTURE_MAP.md:71](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:71)). That omits root metadata, `pyproject.toml`, clients, deploy files, agents, skills, and other tracked text. The final pass must grep every tracked file for old dotted and slash paths after shims are removed.

4. **Medium — the sandbox move map has two sources targeting the same file.** It says `backend/sandbox/__init__.py` stays ([RESTRUCTURE_MAP.md:208](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:208)) while also moving `backend/services/sandbox/__init__.py` onto it ([RESTRUCTURE_MAP.md:214](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/RESTRUCTURE_MAP.md:214)). Both sources exist, so that `git mv` is impossible. The legacy package initializer must instead become the transitional shim and later be deleted.

### Requested adjudications

- **Pure move:** Correct. Four payload files are identical blobs; the 99%-similarity [feed.py:22](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/backend/feed/feed.py:22) is byte-identical after exactly three import-path substitutions. No production logic changed.

- **Sweep judgment:** Correct, with a wording qualification. `CONTROL_MODULES` preserves exactly the old four-file membership ([test_plane_layout.py:42](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/tests/structure/test_plane_layout.py:42)). The process-spawn and BaseStateStore sweeps are not literally membership-identical: they additionally cover the three former domain leaves and the new package shell ([test_plane_layout.py:400](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/tests/structure/test_plane_layout.py:400), [test_service_layout.py:727](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/tests/structure/test_service_layout.py:727)). That is sound strengthening. Omitting the sandbox-consumer checks is sound, and the legacy `DOMAIN_ROOT` sweep may omit FEED because module-law enforcement replaces that physical-layer classification; `feed_policy` retains its explicit leaf assertion.

- **`assertNotIn` → `assertIn`:** Correct. `_import_module_names` records `ImportFrom.node.module` but not imported aliases ([test_service_layout.py:55](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/tests/structure/test_service_layout.py:55)). Therefore the deleted `"feed_policy"` assertion was vacuous for `from ..domain import feed_policy`.

- **Expected import name `services.feed` → `feed.feed`:** Correct at [test_service_layout.py:993](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/tests/structure/test_service_layout.py:993).

- **MODULE_BOUNDARIES row:** Correct at [MODULE_BOUNDARIES.md:39](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/docs/MODULE_BOUNDARIES.md:39).

- **Stale method name:** Correctly deferred; only its name is historical at [test_service_layout.py:600](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/tests/structure/test_service_layout.py:600).

### References and import law

No executable reference was missed. Surviving old FEED paths occur only as intentional examples/move-map entries in `RESTRUCTURE_MAP.md`.

The lazy imports in [local_data_plane.py:201](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/mcp_server/local_data_plane.py:201) target distinct local filesystem adapters, [dataplane/feed_images.py:13](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/backend/dataplane/feed_images.py:13) and [dataplane/feed_embeds.py:13](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/backend/dataplane/feed_embeds.py:13). They did not move.

The computed FEED edges remain `feed→feed` and `feed→kernel`; allowed edges remain `feed→{feed,kernel,object_storage}` at [test_module_boundaries.py:101](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/tests/structure/test_module_boundaries.py:101). Current violations and `GRANDFATHERED` are empty, and nothing non-surface may import surface.

### Integration/de-shim reminders

- Merge `tests/paths.py`, the three structure-test files, classifier tables, docs, and blame metadata additively; all parallel branches touch these.
- Ensure kernel rewrites land in the newly moved FEED files—[feed.py:29](/Users/guraltoo/Documents/dev/proj/experiments/research-suite/.claude/worktrees/agent-ad768136526190a24/merv/backend/feed/feed.py:29) still uses legacy `state`/`utils` paths pending the kernel shim.
- Keep legacy shim paths classified until deletion and test old/new module identity in both import orders.
- After de-shim, globally sweep all tracked files, build/install a wheel, run entry-point imports, then run the full suite and update stale docs/test names.

Focused structural execution ran 96 tests: 94 passed; two helper self-tests errored only because the read-only environment provided no writable temporary directory. The worktree remains untouched.

## kernel (T3)
Verdict: **CORRECTIONS REQUIRED** — only low-severity bookkeeping/documentation corrections. The move and shim implementation itself is sound.

## Findings

- **P3 — `merv/backend/kernel/env.py:24`: the promised de-shim reversal is not recorded.**  
  The comment explains why the logger is pinned, but does not say to restore `logging.getLogger(__name__)` during de-shim. It also names only `tests/state/test_env.py`; `merv/tests/surface/test_brain_dirs.py:141` pins `"backend.env"` too. No tracked document or commit-message text records the restore instruction.

- **P3 — stale physical-path documentation.**  
  `merv/docs/MODULE_BOUNDARIES.md:34` still maps kernel code to `state/*`, `ports/*`, `utils`, etc., despite calling itself the implemented mapping. `merv/docs/CONTROL_DATA_PLANE_SPLIT.md:44` likewise says `state/*`. These should say `kernel/state/*`, `kernel/ports/*`, and `kernel/{utils,env,version,secret_tokens}`. `merv/tests/structure/test_module_boundaries.py:61` also mentions the old `state/store.py` path.

## Adjudication

1. Pure-move discipline: clean.

- 957fc94 has the single parent 755d3e5.
- All five state leaves and all ten ports leaves are R100 renames.
- `state/__init__.py`, `ports/__init__.py`, `utils.py`, and `secret_tokens.py` are byte-identical at their canonical destinations.
- `kernel/env.py` differs only at the logger assignment.
- `kernel/version.py` differs only by `from . import __version__` → `from .. import __version__`.
- The six old paths contain only the declared shims.
- The new `kernel/__init__.py` is a package marker/docstring.
- Test/classification changes match the declared path pins: `paths.py`, three plane-layout hunks, five service-layout pins, module classification, and `test_tool_calls.py:227`.
- No undeclared source changes; `git diff --check` is clean.

2. Package shims: sound in both import orders.

State pre-registers every leaf:

- `activity`
- `dialects`
- `store`
- `tool_call_stats`
- `tool_calls`

Ports pre-registers every leaf:

- `mgmt_keys`
- `object_store`
- `quota_admission`
- `reflection_writers`
- `resource_records`
- `review_policy`
- `sandbox_lifecycle`
- `sandbox_worker`
- `task_channel`
- `workflow_readers`

Canonical-first imports reuse those module objects when the legacy package later registers aliases. Legacy-first imports load the canonical package and children before installing the old keys. No child imports back through the legacy package, so there is no cycle. The extra eager `state.dialects` import is safe: `psycopg` remains function-local/lazy. Ports leaves are definition-only and stdlib-only.

3. Logger pin: correct current behavior.

Pinning `"backend.env"` preserves the existing operator-facing logger and the five assertions at:

- `merv/tests/state/test_env.py:85,91,97,101`
- `merv/tests/surface/test_brain_dirs.py:141`

The missing piece is only the explicit de-shim reminder described above.

4. Import-law/classification: clean.

A static reproduction of the branch classifier found:

- 211/211 backend Python files classified
- zero unclassified files
- zero stale `FILE_MODULES` or `PACKAGE_MODULES` entries
- zero backend boundary violations
- zero kernel-to-non-kernel backend edges
- `GRANDFATHERED = frozenset()`

Within the enforced backend graph, kernel imports only kernel.

## De-shim checklist

The following are intentional legacy consumers today, not current runtime failures.

### Production imports

- `backend.env` — `merv/backend/composition/control_mode.py:41`; `config.py:25`; `execution/__init__.py:12`; `execution/backends/{digitalocean/config.py:7,hyperstack/config.py:7,lambda_labs/config.py:9,modal/config.py:10,modal/sandbox_backend.py:24,tensordock/config.py:7,thunder_compute/config.py:10,verda/config.py:7,voltage_park/config.py:7}`; `execution/vm_ssh.py:11`; `mlflow/config.py:14`; `services/sandbox/{sandbox_daemons.py:15,sandboxes.py:51}`; `transport/http_server.py:19`.

- `backend.ports` — `merv/backend/artifacts/resources.py:14`; `control/control_app.py:20`; `dataplane/resource_observer.py:14`; `services/{quotas.py:16,reflections.py:53,reviews.py:36,workflow.py:25}`; `services/sandbox/{sandbox_daemons.py:16,sandbox_lifecycle.py:26,27,sandbox_metrics.py:9,sandbox_runs.py:18,sandboxes.py:27,28,29,30}`; `storage/{s3_object_store.py:10,service.py:10}`.

- `backend.secret_tokens` — `merv/backend/services/reviews.py:9`.

- `backend.state` — `merv/backend/artifacts/{pinned.py:18,resources.py:16}`; `config.py:29,291,295`; `control/{control_app.py:27,control_runtime.py:11,12,record_core.py:21}`; `observability.py:17`; `services/{association_targets.py:9,claims.py:10,experiments.py:33,feed.py:29,graph_refs.py:8,project_overview.py:13,projects.py:10,quotas.py:17,reflections.py:58,reviews.py:37,workflow.py:32}`; `services/sandbox/{sandbox_registry.py:18,sandbox_runs.py:19,sandboxes.py:12,13}`; `storage/service.py:13`; `tools/tool_facade.py:18`; `transport/api/{app.py:17,views.py:16}`.

- `backend.utils` — `merv/backend/artifacts/{pinned.py:19,resources.py:13}`; `composition/control_mode.py:56`; `config.py:26`; `control/{control_client.py:26,control_runtime.py:20,record_core.py:24}`; `dataplane/{experiment_folders.py:8,feed_embeds.py:9,feed_images.py:9,repo_paths.py:10,resource_artifacts.py:17,resource_observer.py:15,resource_validation.py:26,sandbox_outputs.py:10}`; `domain/{experiment_names.py:7,experiment_policy.py:7,gates.py:9,paths.py:5,reflection_artifacts.py:26,sandbox_paths.py:7}`; `mlflow/{config.py:15,exhibit.py:21}`; `observability.py:18`; `sandbox/{managed_mgmt_keys.py:14,sandbox_support.py:16}`; `services/{association_targets.py:10,claims.py:8,9,11,cleanup.py:29,experiments.py:34,35,36,feed.py:30,permissions.py:17,projects.py:8,9,11,quotas.py:18,reflections.py:59,reviews.py:10}`; `services/sandbox/{sandbox_heartbeat.py:9,sandbox_metrics.py:15,sandbox_provisioner.py:31,sandbox_registry.py:19,sandbox_runs.py:20,sandboxes.py:14}`; `ssh_keys.py:9`; `storage/{blobs.py:29,file_transfer.py:13,s3_blobs.py:26,s3_object_store.py:12,service.py:20}`; `tools/{exhibits.py:24,tool_facade.py:19,20,tool_handlers.py:15}`; `transport/api/{app.py:19,claims.py:12,experiments.py:12,meta.py:12,projects.py:13,reflections.py:12,resources.py:12,reviews.py:12,sandboxes.py:12,sdk_auth.py:21,storage.py:12,views.py:17}`; `transport/{data_plane_http.py:14,feed_http.py:19,mcp_http.py:12}`; `workspace.py:9`.

- `backend.version` — `merv/backend/transport/api/{app.py:26,claims.py:13,experiments.py:13,meta.py:13,projects.py:14,reflections.py:13,resources.py:13,reviews.py:13,sandboxes.py:13,storage.py:13}`.

### Tests, scripts, strings, and patch targets

- `backend.env` — `merv/research_plugin_shared/client_config.py:3`; `tests/state/test_env.py:7,85,91,97,101,106`; `tests/surface/test_brain_dirs.py:141`; plus the logger pin at `backend/kernel/env.py:24`.

- `backend.ports` — `merv/tests/fakes.py:246,304`; `tests/sandbox/test_quotas.py:18`; `tests/structure/test_service_layout.py:352,379,409,518`.

- `backend.secret_tokens` — `merv/tests/state/test_secret_tokens.py:7`; mock-patch targets at `:12,36`.

- `backend.state` — `merv/pyproject.toml:45`; `scripts/_feed_demo_server.py:20`; `tests/state/{test_activity.py:10,test_postgres_dialect.py:44,45,test_project_dirs.py:25,test_store_migrations.py:9,test_tool_calls.py:10,11}`; `tests/storage/test_storage_ledger.py:12`; `tests/structure/test_service_layout.py:798`; `tests/support/brain.py:14`; `tests/surface/{test_control_app.py:30,test_storage_http.py:19}`; `tests/workflow/test_review_policy.py:10`.

- `backend.utils` — `merv/tests/fakes.py:63,84,128,149,180,220,247,292`; all matching imports under `tests/sandbox` at `test_control_task_channel.py:6`, `test_feed_embed_reader.py:9`, `test_feed_image_reader.py:8`, `test_mgmt_keys.py:25`, `test_multiplexer.py:248`, `test_quotas.py:21`, `test_resource_artifact_reader.py:10`, `test_resource_observer.py:9`, `test_sandbox_decomposition.py:37`, `test_sandbox_heartbeat.py:15`, `test_sandbox_outputs.py:9`, `test_sandbox_service.py:13`, `test_ssh_keys.py:10`, `test_task_channel.py:12`; `tests/state/{test_blob_store.py:15,test_identity.py:7,test_mlflow_tracking.py:14,test_postgres_dialect.py:52,test_project_dirs.py:26,test_resource_versions.py:9,test_utils.py:6}`; `tests/storage/{test_object_store_contract.py:13,test_storage_ledger.py:14}`; `tests/support/brain.py:21`; `tests/surface/{test_control_app.py:33,test_control_plane_contract.py:33,test_http_api.py:15,test_mode_config.py:27,test_review_identity.py:11,test_split_mode_smoke.py:19,test_storage_http.py:22,test_tenancy.py:22}`; `tests/workflow/{test_experiment_naming.py:12,test_experiment_writers.py:10,test_feed.py:22,test_metrics_exhibit.py:23,test_project_tools.py:11,test_reflection_gates.py:15,test_review_policy.py:11,test_system_transitions.py:23,test_workflow_gates.py:15,1189}`.

- `backend.version` — `merv/mcp_server/proxy.py:696`; `tests/surface/{test_auth.py:29,test_control_app.py:34,test_mode_config.py:463,484,508}`.

- Bare old filesystem documentation — `merv/docs/MODULE_BOUNDARIES.md:34`; `docs/CONTROL_DATA_PLANE_SPLIT.md:44`; `tests/structure/test_module_boundaries.py:61`.

No legacy dynamic-import target was found. The only legacy mock-patch strings are the two `backend.secret_tokens` targets above.

Finally, delete the six shims; remove `PACKAGE_MODULES["state"]` and `["ports"]`; remove root `FILE_MODULES` entries for `utils.py`, `env.py`, `version.py`, and `secret_tokens.py`; restore the logger to `__name__` with all five logger assertions; then require a zero-result legacy-path grep. No files or tests were modified or run.

## object_storage (T2)
## Verdict

**CORRECTIONS REQUIRED.**

The partial pre-seeding design does not preserve module identity for `service` or `s3_blobs`. The move/content discipline, depth fixes, classification, and import law otherwise pass static review.

## Findings

### High — lazy legacy imports execute canonical files under a second module name

`merv/backend/storage/__init__.py:9-14`

After the shim replaces `backend.storage` with the canonical package object, Python still loads a requested dotted module using the requested fullname:

```text
backend.storage.service
```

It finds `object_storage/service.py` through the aliased `__path__`, but executes it as `backend.storage.service`, not `backend.object_storage.service`. Consequently:

```python
importlib.import_module("backend.storage.service") \
    is importlib.import_module("backend.object_storage.service")
# False
```

The same applies to `s3_blobs.py`.

This occurs in either order:

- Canonical-first: canonical module loads, then the legacy import executes a second copy.
- Shim-first: legacy copy loads first, then a canonical import executes another copy.
- Because both package names reference the same package object, its `service`/`s3_blobs` attribute can also flip to whichever spelling imported last, while both `sys.modules` entries retain different objects.

Observable differences include:

- Distinct `StorageLedgerService` classes and `objects_for_experiment` functions (`object_storage/service.py:30,49`).
- Independent mutable `STORAGE_KINDS` and `STORAGE_STATUSES` sets (`service.py:24-25`).
- Distinct `S3BlobStore` classes (`s3_blobs.py:33`).
- `isinstance`, monkeypatching, class serialization, and module-global modifications can disagree between paths.

Present in-tree production consumers use only the legacy names, so the duplication is latent until canonical and legacy imports mix:

- `backend/composition/control_mode.py:53`
- `backend/control/control_app.py:25`
- `backend/control/record_core.py:23`
- `backend/config.py:239`

There are no canonical `service` or `s3_blobs` consumers yet. That does not make the transition sound: the canonical path introduced by this commit must coexist with compatibility imports.

Minimal fix: adopt the lazy meta-path alias finder used by the sandbox tranche, mapping every `backend.storage.*` request to `backend.object_storage.*`. It preserves both laziness and identity. The current pre-seeds may remain, with the finder handling missing names, or the finder can handle all submodules. Add isolated import-order assertions for `service` and `s3_blobs`, plus an assertion that merely importing the shim does not eagerly load them.

Static nuance: importing `s3_blobs.py` itself does not import boto3; boto3 is gated inside `S3BlobStore.__init__` at `s3_blobs.py:48`. Keeping the module absent from `sys.modules` may still be required by the plane-layout contract.

### Low — architecture docs retain the old physical layout

- `merv/docs/MODULE_BOUNDARIES.md:37`
- `merv/docs/CONTROL_DATA_PLANE_SPLIT.md:45`

Both still describe `storage/*` as the implementation location and omit canonical `object_storage/*`. These should describe `object_storage/*` as canonical and the two old paths as transitional shims.

## Import adjudication

| Import | Result |
|---|---|
| `from backend.storage.blobs import BlobStore` | Canonical `backend.object_storage.blobs.BlobStore`; identity preserved in either order. |
| `importlib.import_module("backend.storage.file_transfer")` | Exact canonical module object; identity preserved. |
| `importlib.import_module("backend.domain.storage_guidance")` | Exact canonical guidance module through the self-replacing shim. |
| `import backend.storage.service` | Second execution under the legacy name; identity broken. |
| `import backend.storage.s3_blobs` | Second execution under the legacy name; identity broken. |

The lazy strings in `mcp_server/local_data_plane.py:430,432,502,504,602` are byte-untouched by the commit. The file-transfer and guidance strings resolve to canonical module objects as claimed.

## Other adjudications

- **Three depth fixes:** Correct. `service.py:11-12` and `s3_object_store.py:11` point at their canonical same-package modules. Under legacy execution, their dependencies hit the pre-seeded aliases. They prevent dependency duplication, but do not prevent `service.py` or `s3_blobs.py` themselves from being executed twice.
- **Blob integrity:** Pass. Exact old/new blob matches exist for `__init__`, `blobs`, `file_transfer`, `s3_blobs`, and `storage_guidance`. Reversing only the three stated import substitutions reproduces the original `service.py` blob `e81d431…` and `s3_object_store.py` blob `b07d5c4…`.
- **Pure-move discipline:** Pass at content level. No unrelated production changes or whitespace errors were found.
- **Boundary-graph blind spot:** Confirmed subtractive only. Old imports such as `backend.storage.blobs` have no physical dotted-index entry and are omitted by the AST graph. That can hide real edges but cannot invent violations. The hidden production edges here are either surface edges or the ratified `artifacts -> object_storage` edge.
- **Classification/import law:** Complete. `object_storage` and the legacy `storage` shim are classified as `OBJECT_STORAGE`; the domain shim retains its file override. The only allowances into object storage remain `artifacts -> object_storage` and `feed -> object_storage`. `GRANDFATHERED` is empty.

## De-shim checklist

Update these old-path consumers:

- Production: `backend/artifacts/{pinned,resources}.py`, `backend/composition/control_mode.py`, `backend/config.py`, `backend/control/{control_app,record_core}.py`, `backend/tools/contracts.py`.
- Lazy imports: `mcp_server/local_data_plane.py`.
- Script: `scripts/_feed_demo_server.py`.
- Tests: `tests/fakes.py`, `tests/state/{test_blob_store,test_s3_blob_store}.py`, `tests/storage/{test_object_store_contract,test_storage_ledger}.py`, `tests/support/brain.py`, `tests/surface/{test_control_app,test_storage_http}.py`.
- Docs: the two files identified above.
- No old-path patch strings were found.

Then delete both shims and remove `"storage"` from `PACKAGE_MODULES` and `"domain/storage_guidance.py"` from `FILE_MODULES`. No files were modified, and the suite was not run.

## sandbox (T4)
# Verdict: CLEAR TO INTEGRATE

No blocking, major, or correctness findings. Commit `25ded01` has the stated parent `755d3e5`, and the tranche is behavior-neutral based on the static Git audit.

## Findings

### P3 — de-shim dependency inventory, not a current defect

Eighteen canonical provider modules still import `backend.execution.*` internally:

- `digitalocean/sandbox_backend.py:19-20`
- `hyperstack/sandbox_backend.py:19-20`
- `lambda_labs/sandbox_backend.py:18,22`
- `modal/sandbox_backend.py:25,30,36`
- `tensordock/sandbox_backend.py:20-21`
- `thunder_compute/sandbox_backend.py:12,13,17`
- `verda/sandbox_backend.py:18-19`
- `voltage_park/sandbox_backend.py:23-24`

These are safe now because the finder aliases them, but they are the highest-priority rewrites before deleting `backend/execution/__init__.py`.

### P3 — finder removal requires process restart

Both package shims insert anonymous finder instances at line 43:

- `merv/backend/execution/__init__.py:43`
- `merv/backend/services/sandbox/__init__.py:43`

Deleting the shim files does not remove already-installed finders from a running interpreter. Normal deployment restart is a sufficient cleanup story. If hot de-shimming or import-state-reset tests are required, the integration implementation should retain a named finder handle and provide idempotent install/remove behavior.

### P3 — stale path prose for later cleanup

Several comments/docs still describe the old physical topology:

- `docs/MODULE_BOUNDARIES.md:38`
- `docs/CONTROL_DATA_PLANE_SPLIT.md:43`
- `backend/utils.py:116`
- `backend/sandbox/execution/backends/modal/sandbox_backend.py:73`
- `mcp_server/proxy.py:415`
- `pyproject.toml:45`

These do not affect this pure-move tranche but belong in the de-shim/integration rewrite.

## Meta-path finder adjudication

The finder is correct for both import orders.

- Old-first: importing the old package loads the canonical package, installs the finder, and aliases the package entry. Each old submodule spec then imports the canonical target and replaces the temporary old-name module in `sys.modules`.
- Canonical-first: the canonical object already exists; later old imports resolve to it and install the old-name entry without re-execution.
- Parent-package attributes are assigned by import machinery to the returned canonical module, preserving identity through nested packages such as `backends.fake`.
- Consequently, `mock.patch` resolves the same module object under either spelling.

Laziness is preserved. Importing either package shim loads only:

- the empty `backend.sandbox` package, or
- `backend.sandbox.execution`, which imports `backend.env` and the provider-neutral `sandbox_backend`.

No provider package or Modal/provider SDK is imported until a provider is specifically requested.

Installation is sufficiently thread-safe for normal imports: execution of a given package shim is serialized by Python’s import lock, so concurrent first imports cannot execute the same shim twice. Re-import after manually deleting `sys.modules` entries could install duplicates; that is another reason to centralize an idempotent helper during integration.

Recommendation: unify on the lazy meta-path finder, not the kernel tranche’s pre-registration pattern in `restructure-t3-kernel:merv/backend/state/__init__.py:4-10`. The kernel approach eagerly imports all five state submodules, requires manual enumeration whenever the package changes, and does not naturally scale to nested provider trees. A shared finder helper with an `(old_prefix, new_prefix)` registry and removable handle gives the strongest combination of laziness, identity, nested-package coverage, and cleanup.

## Depth and filesystem audit

All 81 relative-import changes were compared from the original blob to its canonical destination. They resolve correctly. Representative sample: 43 changed lines across all requested categories.

| Category | Sampled locations | Resolution |
|---|---|---|
| Execution root | `execution/__init__.py:12-13` | `...env` → `backend.env`; `..sandbox_backend` → sandbox sibling |
| Provider leaves | `digitalocean/client.py:10`, `config.py:7-8`, `sandbox_backend.py:21`; `fake/__init__.py:13`; `modal/_sandbox_ops.py:11`, `config.py:10-11`, `sandbox_backend.py:41,55`; `tensordock/client.py:15`, `config.py:7-8`; `thunder_compute/client.py:10`, `config.py:10-11`, `sandbox_backend.py:21` | Five dots correctly reach `backend`; four reach `backend.sandbox`; unchanged three-dot imports remain within `sandbox.execution` |
| Execution support | `vm_ssh_backend.py:18`, `multiplexer.py:24`, `sync_dirs.py:5` | Correctly reach `backend.sandbox.sandbox_backend` or `sandbox_paths` |
| Former services | `sandbox_daemons.py:14-16,19`; `sandbox_heartbeat.py:8-9`; `sandbox_lifecycle.py:26,28`; `sandbox_metrics.py:9-10`; `sandbox_provisioner.py:30`; `sandbox_registry.py:18`; `sandbox_runs.py:19`; `sandbox_views.py:20`; `sandboxes.py:10,12,27,36,51` | Single-dot imports now address sandbox siblings; two-dot imports reach backend kernel/ports/state/utilities |
| Singles | `ssh_keys.py:9`, `transcript_cache.py:24` | Correctly changed to `..utils` and `.sandbox_backend` |

`quotas.py` and `sandbox_paths.py` are byte-identical to their originals because both old and new packages use the same `..` depth.

The two anchors are correct:

- `modal/config.py:222`
- `thunder_compute/config.py:118`

The new path adds one directory level, so `parents[5]` still resolves to `merv/`, exactly where old `parents[4]` resolved. A complete search of the moved sources found no other `Path(__file__).parents[...]` or equivalent `__file__` anchors.

Every rename below 97% was inspected line by line:

- DigitalOcean, TensorDock, Verda, and Voltage Park configs: only the two expected imports.
- `sync_dirs.py`: only `domain.sandbox_paths` → sibling `sandbox_paths`.
- `sandbox_daemons.py`: four correct import retargets.
- `sandbox_metrics.py`: four correct import retargets.

## Pure-move and classification discipline

The four singleton payloads are either byte-identical or differ only by the required relative import. All other moved content changes are import depth or the two anchors.

The three flagged test deviations are minimal and neutral:

- `test_sandbox_decomposition.py`: canonical facade path and `parents[3] → parents[2]`, both still producing the same `merv/` `PYTHONPATH`.
- `test_sandbox_backend_contract.py:203`: reads the canonical daemon source.
- `test_system_transitions.py:112`: reads the canonical sandbox facade.

The classification table is byte-unchanged. Coverage is correct:

- All canonical Python files match the `sandbox` prefix.
- Package shims match `execution` and `services/sandbox`.
- File shims match the existing four exact entries.
- Every shim-to-canonical edge remains SANDBOX → SANDBOX.
- `GRANDFATHERED` remains empty.

## De-shim checklist

1. Retarget the 18 canonical provider imports listed above.
2. Retarget production consumers:
   - `composition/control_mode.py:42`
   - `control/control_app.py:24`
   - `control/record_core.py:17`
   - `sandbox/mgmt_keys.py:18`
3. Rewrite four scripts containing six `backend.execution` imports.
4. Rewrite tests:
   - 93 `backend.execution` occurrences across 47 files.
   - 11 `backend.services.sandbox` occurrences across three files.
   - `test_quotas.py:20`.
   - `test_transcript_cache.py:16`.
   - Five `backend.ssh_keys` occurrences in `test_ssh_keys.py`, including four patch strings.
5. Update the stale prose/comments listed above. No exact `backend.domain.sandbox_paths` dotted reference remains.
6. Delete the four file shims and two package shims.
7. Remove `execution` and `services/sandbox` from `PACKAGE_MODULES`, and the four old singleton entries from `FILE_MODULES`.
8. Restart processes to clear installed finders, then confirm the six old prefixes have zero remaining references.

No files were modified and the suite was not run.

## research_core (T5)
## Verdict

**CLEAR TO INTEGRATE.**

No high- or medium-severity findings. The only low-severity issue is the acknowledged temporary lint-coverage gap; direct inspection confirms it hides no present violation.

No files were modified and no suite was run.

## Findings

- **Low — deferred lint coverage:** `merv/tests/structure/test_plane_layout.py:43,46-64,398,794` and `merv/tests/structure/test_service_layout.py:649,741` still scan old-path shims instead of some real `research_core` files. This weakens future regression detection during the shim window but does not invalidate this commit.

## Static adjudication

1. Pure-move discipline: passes.

- `1c26a20` has the single claimed parent, `755d3e5`.
- The 63 files decompose exactly into 31 additions, 29 old files replaced with four-line shims, and three structure-test changes.
- All 13 service destinations have identical Git blob hashes to their `main` source files.
- All 16 domain comparisons are identical except the ten declared import-depth changes.
- All 29 shims consistently import the destination and replace `sys.modules[__name__]`.
- `backend/services/__init__.py`, `backend/domain/__init__.py`, and all five sibling-owned domain files are byte-identical to `main`.
- `git diff --check` only reports the pre-existing blank EOF line in `claims.py`; its source and moved blobs are identical.

2. Depth fixes: all ten are correct.

- `research_core/domain/artifacts.py:8`
- `experiment_names.py:7`
- `experiment_policy.py:7`
- `gates.py:9`
- `paths.py:5`
- `reflection_artifacts.py:16-18,26`
- `reflection_gates.py:36`

Each changes `..` to `...`, preserving the original `backend.artifacts.*` or `backend.utils` target after gaining one package level. Internal `.domain-module` imports correctly remain single-dot imports.

3. Inventory and classification: exact.

On `main`, `backend/domain` contains precisely:

- 16 moved RESEARCH_CORE files.
- Five overridden sibling files: `feed_policy`, `feed_images`, `feed_embeds` → FEED; `sandbox_paths` → SANDBOX; `storage_guidance` → OBJECT_STORAGE.
- `__init__.py`, inherited as RESEARCH_CORE.

The 13 moved service files are exactly the 13 RESEARCH_CORE `FILE_MODULES` entries. On the branch, `PACKAGE_MODULES["research_core"]` completely classifies both new packages, while `domain` and the old service entries correctly classify the transitional shims.

4. Covered invariants: current files pass direct inspection.

- All moved domain modules avoid `composition`, `dataplane`, `execution`, `services`, `state`, and `workspace`.
- No moved file references `subprocess`, asyncio subprocess creation, or `os` process-spawn APIs.
- No moved service imports `StateStore` or `SqliteStateStore`; store users use `BaseStateStore`. `association_targets.py` imports only the neutral `Connection` protocol.
- The moved CONTROL_MODULES candidates import none of `dataplane`, `sandbox_conn`, `subprocess`, or `workspace`.
- No moved file imports `LocalMgmtKeyStore` or its adapter.

5. Import law: intact.

- New and old shim files are completely classified.
- RESEARCH_CORE’s only cross-module production imports are the ratified ARTIFACTS imports; remaining dependencies are RESEARCH_CORE or KERNEL.
- `ALLOWED_EDGES` was not broadened.
- `GRANDFATHERED` remains empty.
- Moving files changes no module ownership edge: old services and domain files were already classified RESEARCH_CORE.

## Exhaustive de-shim references

Executable service-path rewrites:

- `merv/backend/control/control_app.py:26`
- `merv/backend/control/record_core.py:9-20`
- `merv/backend/tools/tool_handlers.py:14`
- `merv/tests/structure/test_plane_layout.py:560-562,639-640`
- `merv/tests/structure/test_service_layout.py:302,569-571,636-637`

Executable domain-path rewrites:

- Inside the new package, change old `..domain.*` imports to `.domain.*` at:
  - `research_core/claims.py:7`
  - `experiments.py:8-25`
  - `project_overview.py:8,10`
  - `reflections.py:18-52`
  - `reviews.py:24-35`
  - `workflow.py:13-20`
  - `workflow_views.py:7`
- External production consumers:
  - `backend/dataplane/resource_validation.py:8-21`
  - `backend/services/auth.py:22`
  - `backend/services/identity.py:12`
  - `backend/services/permissions.py:16`
  - `backend/tools/contracts.py:13`
  - `backend/tools/exhibits.py:16`
  - `backend/transport/api/views.py:13`
- Tests:
  - `tests/state/test_blob_store.py:11-12`
  - `tests/structure/test_service_layout.py:1182,1232,1243`
  - `tests/workflow/test_experiment_writers.py:8-9`
  - `test_logic_graph.py:6`
  - `test_reflection_gates.py:10-11`
  - `test_review_gate_policy.py:5-12`
  - `test_review_return_policy.py:5`
  - `test_review_snapshot.py:5`
  - `test_synopsis_policy.py:5`
  - `test_system_transitions.py:17`
  - `test_workflow_gates.py:10-11`

No moved-name `mock.patch`, `importlib.import_module`, `__import__`, `find_spec`, or script references were found.

Documentation/comment references to refresh:

- `backend/research_core/domain/reflection_gates.py:19`
- `backend/research_core/domain/review_snapshot.py:3`
- `backend/utils.py:115`
- `backend/tools/tool_handlers.py:25`
- `docs/ARCHITECTURE.md:169,186`
- `docs/CONTROL_DATA_PLANE_SPLIT.md:42`
- `docs/MCP_SERVER_CONTRACT.md:147`
- `docs/MODULE_BOUNDARIES.md:35`
- `research_state_ui/src/utils/planSections.js:7`
- `tests/structure/test_module_boundaries.py:62`

The `domain.*` literals at `test_service_layout.py:540,551,623` are AST suffix expectations for eventual `.domain.*` imports and need not change.

## De-shim checklist

- Delete the 29 shims.
- Remove `FILE_MODULES` entries at `test_module_boundaries.py:67-79`; update the `domain` package comment at line 38.
- Retarget the ten moved entries in `CONTROL_MODULES` to `RESEARCH_CORE_ROOT`.
- Extend `DOMAIN_MODULES` to include `research_core/domain/*.py`.
- Extend:
  - process-spawn scan at `test_plane_layout.py:398`
  - management-key-adapter scan at `test_plane_layout.py:794`
  - domain-independence scan at `test_service_layout.py:649`
  - BaseStateStore scan at `test_service_layout.py:741`
- Rewrite the consumer and documentation sites listed above.
- Update the module-package mapping documentation to name `research_core/*` and `research_core/domain/*`.
