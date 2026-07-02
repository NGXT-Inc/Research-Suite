# Research Plugin Improvement Progress

Date started: 2026-07-02

## Scope

Work through the hands-on improvement requests from
`improvement_requests/2026-07-02_research_plugin_improvement_requests.md`,
making small, verified commits inside the Research Plugin repo.

## Batch 1: storage file helpers

Status: complete

Request addressed:

- Make storage easier to use end-to-end.

Implementation notes:

- Added `storage.upload_file` as a data-plane helper that hashes a local file,
  registers the storage object, streams bytes to the presigned upload target,
  and completes the ledger object.
- Added `storage.download_file` as a data-plane helper that resolves a storage
  object, downloads to a temp file, verifies sha256 and size, then atomically
  replaces the destination.
- Kept existing low-level `storage.put_object`, `storage.complete_upload`, and
  `storage.resolve` primitives for hosted/control-plane flows.
- Relative helper paths resolve against the project repo root in local mode.

Verification:

- `PYTHONPATH=. python -m unittest tests.storage.test_storage_ledger -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.surface.test_storage_http -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_local_shipping.LocalShippingTest.test_mcp_launcher_uses_current_repo_for_state_and_resources -v`
- `PYTHONPATH=. python -m unittest tests.structure.test_plane_layout.ToolPlanePartitionTest tests.storage.test_storage_ledger tests.surface.test_storage_http tests.surface.test_tool_contracts -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (841 tests, 25 skipped)

Follow-up candidates:

- Surface storage object associations more clearly in experiment state.
- Add sandbox output retention helpers that can choose storage automatically for
  large files.

## Batch 2: batch resource association

Status: complete

Request addressed:

- Batch resource association.

Implementation notes:

- Added `resource.associate_batch` as a data-plane helper over the existing
  `resource.associate` path.
- Rows are applied in order and preserve the same role validation,
  gated-artifact byte capture, and attempt scoping as single associations.
- Added split-mode daemon support so the tool is served anywhere
  `resource.associate` is served.

Verification:

- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates.WorkflowGateTest.test_resource_associate_batch_satisfies_results_gate -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest.test_daemon_catalog_only_advertises_implemented_data_tools -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (846 tests, 25 skipped)

## Batch 3: gated artifact preflight lint

Status: complete

Request addressed:

- Preflight linter for gated artifacts.

Implementation notes:

- Added `resource.validate` as a data-plane helper that reads the current local
  repo file without registering or associating it.
- The validator reports file/path errors, gated-role byte caps, required plan
  sections, report structure and figure availability, and graph envelope
  problems before transition gates are attempted.
- Wired the tool through local mode and split-mode daemon routing, with docs
  updates for the MCP contract and control/data-plane split.

Verification:

- `PYTHONPATH=. python -m unittest tests.sandbox.test_resource_artifact_validation -v`
- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates.WorkflowGateTest.test_resource_validate_preflights_plan_before_association -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest.test_resource_validate_reads_local_file_without_control_mutation tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest.test_daemon_catalog_only_advertises_implemented_data_tools -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (854 tests, 25 skipped)

## Batch 4: safe TSV results merge

Status: canceled

Request addressed:

- Protect `results.tsv` from clobbering.

Implementation notes:

- Originally added `results.merge_tsv` as a generic data-plane helper, then
  canceled it because `results.tsv` is specific to the current research
  project and should not become a universal plugin concept.
- The cancellation removes the tool contract, local handler, daemon route,
  dataplane helper, feature tests, and docs for generic TSV ledger merging.

Verification:

- Historical pre-cancellation verification is superseded by the cancellation
  batch below.

## Batch 5: explicit experiment folder materialization

Status: complete

Request addressed:

- Create local experiment folders when experiments materialize.

Implementation notes:

- Added `experiment.materialize_folders` as a data-plane helper that creates
  canonical local `experiments/<name>/` folders without changing experiment
  state.
- The tool defaults to planned experiments, matching the reflection-publish
  case where a new wave appears in MCP before local folders exist; callers can
  pass `experiment_id` for one folder or `status: null` for all experiments.
- Preserved existing lazy `experiment.create` semantics and fixed the MCP
  contract wording that incorrectly claimed folders were created immediately.
- Wired the tool through local mode and split-mode daemon routing.

Verification:

- `PYTHONPATH=. python -m unittest tests.surface.test_experiment_materialize_folders -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest.test_experiment_materialize_folders_uses_linked_project tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest.test_daemon_catalog_only_advertises_implemented_data_tools -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest -v`
- `PYTHONPATH=. python -m unittest tests.structure.test_plane_layout.PlaneImportLintTest.test_app_import_keeps_local_io_modules_unloaded -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (866 tests, 25 skipped)

## Batch 6: experiment gate checklist

Status: complete

Request addressed:

- Show review gate checklist in experiment state.

Implementation notes:

- Added `gate_checklist` to experiment state, derived from the same
  `GATE_TABLE` that drives workflow enforcement and `allowed_transitions`.
- Checklist items show resource/review requirements for the current forward
  transition, including missing artifacts, pending/requested/started review
  state, and ready status.
- Validator-backed artifacts (`plan`, `report`, `graph`) run the same
  pinned-byte lints used by transitions, so invalid submitted artifacts appear
  as checklist problems before the transition is attempted.
- Preserved review snapshot stability because snapshot ids already use a
  field-limited equality key.

Verification:

- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates.WorkflowGateTest.test_get_state_surfaces_allowed_transitions_with_requirements tests.workflow.test_workflow_gates.WorkflowGateTest.test_terminal_experiment_has_no_allowed_transitions tests.workflow.test_workflow_gates.WorkflowGateTest.test_ready_guidance_pre_lints_the_graph tests.workflow.test_workflow_gates.WorkflowGateTest.test_pending_review_allows_fresh_request_for_lost_capability tests.workflow.test_experiment_slim -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest tests.structure.test_plane_layout.PlaneImportLintTest -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_control_plane_contract tests.surface.test_http_api -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (866 tests, 25 skipped)

## Batch 7: storage object visibility in experiment state

Status: complete

Request addressed:

- Associate storage objects with experiments and reports more visibly.

Implementation notes:

- Surfaced a compact `storage_objects` list on experiment state for
  non-deleted durable storage objects whose `producing_experiment_id` matches
  the experiment.
- Kept the visibility path tied to existing storage ledger metadata rather
  than adding a second association model.
- The agent-facing `experiment.get_state` projection keeps stable fields such
  as id, name, version, kind, checksum, size, status, expiry, run id, source
  URI, and notes while omitting internal storage namespace details.
- Added docs for both MCP and UI consumers so reviewers can find retained
  checkpoints, logs, datasets, and similar heavy artifacts directly from
  experiment state.

Verification:

- `PYTHONPATH=. python -m unittest tests.surface.test_storage_http.StorageHttpApiTest.test_experiment_state_surfaces_produced_storage_objects tests.workflow.test_experiment_slim.ExperimentSlimTest.test_get_state_tool_is_slim -v`
- `PYTHONPATH=. python -m unittest tests.storage.test_storage_ledger tests.surface.test_storage_http tests.workflow.test_experiment_slim -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest tests.structure.test_plane_layout.PlaneImportLintTest -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (867 tests, 25 skipped)

## Batch 8: storage usage guidance

Status: complete

Request addressed:

- Give clearer guidance on what belongs in storage.

Implementation notes:

- Added one shared storage guidance policy covering what belongs in durable
  storage, what should stay as repo resources, and what can remain ephemeral.
- Surfaced that policy from `storage.list`, storage tool descriptions, workflow
  result-retention guidance, sandbox request hints, and sandbox release
  retention warnings.
- Documented the same "what belongs where" rule in `docs/STORAGE_MODEL.md`.

Verification:

- `PYTHONPATH=. python -m unittest tests.storage.test_storage_ledger.StorageLedgerServiceTest.test_default_list_only_returns_available_objects tests.surface.test_tool_contracts.ToolContractRegistryTest.test_storage_tools_registered_with_expected_input_models -v`
- `PYTHONPATH=. python -m unittest tests.storage.test_storage_ledger tests.surface.test_storage_http tests.sandbox.test_sandbox_service -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest tests.structure.test_plane_layout.PlaneImportLintTest -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (868 tests, 25 skipped)

## Batch 9: reviewer capability recovery guidance

Status: complete

Request addressed:

- Reviewer capability recovery.

Implementation notes:

- Added non-secret `recovery` metadata to review request records returned by
  `review.status` and the review queue.
- The recovery block makes the one-time capability behavior explicit:
  capabilities are not recoverable from state, but open requests can be
  refreshed by calling `review.request` again for the same target and role.
- Preserved the existing trust model: plaintext reviewer capabilities are still
  returned only at creation time, and no stored capability material is exposed.
- Documented the recovery shape in the MCP contract and updated the
  `review.status` tool description.

Verification:

- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates.WorkflowGateTest.test_pending_review_allows_fresh_request_for_lost_capability tests.surface.test_tool_contracts.ToolContractRegistryTest.test_registered_tools_match_contracts_and_have_descriptions -v`
- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates tests.workflow.test_synthesis_gates tests.surface.test_tenancy -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.surface.test_http_api -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (868 tests, 25 skipped)

## Batch 10: MLflow context in experiment state

Status: complete

Request addressed:

- Surface MLflow links directly in experiment state.

Implementation notes:

- Added a shared MLflow visibility predicate for experiment statuses at
  `running` or later: `running`, `experiment_review`, `complete`, and `failed`.
- `experiment.get_state` now includes the central experiment-scoped `mlflow`
  block and `mlflow_guidance` once the experiment reaches a visible status,
  without querying MLflow or creating runs.
- The HTTP experiment state endpoint now includes the same `mlflow` context for
  UI navigation once the experiment is running or later.
- Kept pre-run state quiet: `ready_to_run` still omits MLflow context, while
  `experiment.transition(start_running)` continues to hand back the connection
  block at the moment a run begins.
- Updated MCP, UI, centralized MLflow, and tool-contract docs to describe the
  state-level surface.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.surface.test_http_api.ResearchPluginHttpApiTest.test_running_transition_and_tool_hand_mlflow_block tests.state.test_mlflow_tracking tests.surface.test_tool_contracts tests.structure.test_plane_layout.PlaneImportLintTest -v` (51 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (868 tests, 25 skipped)

## Batch 11: review request-and-start helper

Status: complete

Request addressed:

- Reduce review boilerplate.

Implementation notes:

- Added `review.request_and_start` as a control-plane helper that creates a
  review request and immediately starts a read-only reviewer session.
- The helper returns request/session metadata and a `review_session_id`, but
  intentionally does not expose the one-time `reviewer_capability` in its
  response.
- `workflow.status_and_next` now advertises `review.request_and_start` at
  review gates alongside the lower-level `review.request` path.
- Added upfront validation so the helper rejects equal producer/reviewer
  session ids before minting a capability, avoiding a dangling lost request.
- Documented when to use the composed helper versus the manual
  `review.request` / `review.start` split.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates.WorkflowGateTest.test_review_request_and_start_opens_read_only_session tests.workflow.test_workflow_gates.WorkflowGateTest.test_review_request_and_start_rejects_same_session_before_mint tests.workflow.test_workflow_gates.WorkflowGateTest.test_pending_review_allows_fresh_request_for_lost_capability tests.surface.test_tool_contracts tests.structure.test_plane_layout.PlaneImportLintTest -v` (43 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (871 tests, 25 skipped)

## Batch 12: claim update suggestions after completion

Status: complete

Request addressed:

- Auto-suggest claim updates after reviewed completion.

Implementation notes:

- Completed experiments with tested claims and a persisted conclusion now
  include `claim_update_suggestions` in experiment state.
- Each suggestion is a scoped `claim.update` call skeleton with `project_id`
  and `claim_id`, plus current claim metadata and the experiment conclusion.
- Added conservative deterministic status inference from conclusion text for
  `supported`, `weakened`, and `contradicted`; unclear conclusions leave
  `suggested_status` unset rather than guessing.
- Kept the behavior advisory: suggestions require confirmation and never mutate
  claims automatically.
- Preserved the suggestions in the agent-facing `experiment.get_state` slim
  projection and documented the shape for MCP/UI consumers.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates.WorkflowGateTest.test_complete_suggests_scoped_claim_update_for_negative_result tests.workflow.test_workflow_gates.WorkflowGateTest.test_complete_suggests_scoped_claim_update_for_supported_result tests.workflow.test_experiment_slim -v` (7 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (873 tests, 25 skipped)

## Batch 13: project active sandbox reuse

Status: canceled

Request addressed:

- Add a first-class project active VM concept so agents keep the same VM across
  experiments by default.

Implementation notes:

- The project-wide active sandbox reuse default was removed because it can
  attach a new agent to a sandbox another agent is actively using.
- `sandbox.request` only reuses an active sandbox already attached to the
  requested experiment unless the caller explicitly uses a dedicated attach
  flow.
- The tool and backend docs no longer describe project-wide default reuse.

Verification:

- Superseded by the cancellation batch below.

## Batch 14: active sandbox expiry warnings

Status: canceled

Request addressed:

- Provide expiry warnings during active runs.

Implementation notes:

- The expiry-warning surface was removed because these sandbox UX changes are
  being replaced by a cleaner design.
- `workflow.status_and_next` no longer returns `sandbox_expiry` warning entries.
- The MCP contract no longer documents expiry warnings.

Verification:

- Superseded by the cancellation batch below.

## Batch 15: sandbox lifecycle reason surface

Status: canceled

Request addressed:

- Make sandbox lifecycle more explicit and durable.

Implementation notes:

- The lifecycle reason surface was removed because these sandbox UX changes are
  being replaced by a cleaner design.
- The cancellation removes reason-specific terminal row details and the
  `lifecycle_reason` / `lifecycle_detail` fields from the agent-facing sandbox
  surface.

Verification:

- Superseded by the cancellation batch below.

## Batch 16: sandbox output pull helper

Status: complete

Request addressed:

- Automatic artifact retention from sandboxes.

Implementation notes:

- Added `sandbox.pull_outputs` as a data-plane helper that copies selected
  files or directories from a running sandbox's remote `experiment_dir` into
  the local experiment folder over SSH/rsync.
- With no explicit paths, the tool checks for common retained outputs:
  `results/`, `figures/`, `report.md`, `graph.json`, `metrics.json`, and
  `results.json`, then pulls only the paths that exist.
- Existing local files are preserved by default; callers must set
  `overwrite=true` before replacing retained local outputs.
- Wired the tool through local mode and split-mode daemon routing, including
  daemon-side SSH key/local folder enrichment before the rsync step.
- Updated MCP, workflow, and split-mode docs so agents use
  `sandbox.pull_outputs` before resource registration/association or sandbox
  release.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.sandbox.test_sandbox_outputs tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest -v` (33 tests)
- `PYTHONPATH=. python -m unittest tests.structure.test_plane_layout.PlaneImportLintTest -v` (26 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (883 tests, 25 skipped)

## Batch 17: retention guidance uses sandbox.pull_outputs

Status: complete

Request addressed:

- Automatic artifact retention from sandboxes.
- Create local experiment folders when experiments materialize.

Implementation notes:

- Updated the Research Workflow skill so future agents prefer
  `sandbox.pull_outputs` for light retained files instead of raw `rsync`/`scp`
  guidance.
- Clarified that `experiment.create` announces the canonical experiment folder
  and `experiment.materialize_folders` is the data-plane helper to create it
  locally when missing.
- Kept heavy-artifact guidance pointed at durable storage rather than repo
  resources.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.surface.test_plugin_skills -v`

## Batch 18: reflection publish next-step guidance

Status: complete

Request addressed:

- Materialized experiment visibility after reflection publish.

Implementation notes:

- Published reflection waves that create planned experiments now include
  `post_publish_guidance` in the publish response and reflection state.
- The guidance lists each new experiment id/name/folder/status/intent and
  recommends `experiment.materialize_folders(status="planned")` followed by
  `workflow.status_and_next` for the first new experiment.
- Updated the MCP contract and project-reflection skill so agents see the
  handoff immediately after publish instead of hunting through experiment lists.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.workflow.test_synthesis_gates.SynthesisGateTest.test_publish_materializes_claim_changes_and_experiment_wave tests.surface.test_plugin_skills -v`
- `PYTHONPATH=. python -m unittest tests.workflow.test_synthesis_gates -v` (51 tests)

## Batch 19: durable sandbox command status snapshot

Status: complete

Request addressed:

- Persist command status independently of SSH.

Implementation notes:

- Extended transcript marker parsing to produce a structured latest-command
  snapshot: command id, command text, start/finish times, status, exit code, and
  a capped output tail.
- Added sandbox-row columns for the last known command snapshot and persist them
  on every successful `sandbox.terminal` transcript read.
- When a later transcript read fails, `sandbox.terminal` now returns the last
  persisted command snapshot with `command_status_stale: true` instead of
  dropping command status to null.
- Kept backward-compatible top-level fields: `last_exit_code`,
  `last_command_finished_at`, and `command_running`.
- Updated MCP/tool/workflow docs to advertise `last_command` and the stale
  status fallback.

Verification:

- `PYTHONPATH=. python -m unittest tests.sandbox.test_sandbox_service.SandboxServiceTest.test_terminal_parses_successful_exit_code tests.sandbox.test_sandbox_service.SandboxServiceTest.test_terminal_reports_nonzero_exit_code tests.sandbox.test_sandbox_service.SandboxServiceTest.test_terminal_command_running_when_no_exit_marker_yet tests.sandbox.test_sandbox_service.SandboxServiceTest.test_terminal_keeps_last_finished_exit_while_next_command_runs tests.sandbox.test_sandbox_service.SandboxServiceTest.test_terminal_exit_code_survives_since_cursor tests.sandbox.test_sandbox_service.SandboxServiceTest.test_terminal_returns_stale_command_status_when_read_unavailable -v` (6 tests)
- `PYTHONPATH=. python -m unittest tests.sandbox.test_sandbox_service tests.surface.test_tool_contracts tests.structure.test_plane_layout.PlaneImportLintTest -v` (107 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (885 tests, 25 skipped)

## Batch 20: reflection project graph diff

Status: complete

Request addressed:

- Better graph diffing between reflections.

Implementation notes:

- Added `project_graph_diff` to reflection state. When a current wave has a
  submitted project graph and a previous published project graph exists, the
  block compares the pinned graph versions.
- The diff reports base/current reflection ids, graph version ids, a summary,
  and node/edge `added`, `removed`, `changed`, and `unchanged_count` groups.
- If either graph is missing, invalid, or cannot be read from pinned bytes,
  reflection state remains readable and the diff reports `available: false`
  with a reason and problems.
- Updated MCP/UI docs, the `reflection.get` tool description, and the
  project-reflection skill so agents inspect the diff before review.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.workflow.test_synthesis_gates.SynthesisGateTest.test_reflection_state_diffs_project_graph_against_previous_publish -v`
- `PYTHONPATH=. python -m unittest tests.workflow.test_synthesis_gates tests.surface.test_tool_contracts tests.surface.test_plugin_skills -v` (69 tests)
- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_slim tests.surface.test_http_api.ResearchPluginHttpApiTest.test_synthesis_endpoints_and_project_graph tests.structure.test_plane_layout.PlaneImportLintTest -v` (31 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (886 tests, 25 skipped)

## Batch 21: MLflow run identity at experiment start

Status: complete

Request addressed:

- Create the MLflow run at experiment start.

Implementation notes:

- Added durable MLflow run columns to experiment state and project them as a
  compact `mlflow_run` block.
- Added a best-effort `CentralMlflowService.create_run` path that uses the
  backend MLflow write URI to create the MLflow experiment if needed, then
  creates the initial RUNNING run with Research Plugin tags.
- `experiment.transition(start_running)` now attempts that run creation once,
  persists the run id, and returns it through `mlflow.run` plus
  `MLFLOW_RUN_ID` / `RP_MLFLOW_RUN_ID` env vars for resume-in-place logging.
- `experiment.get_state`, `mlflow.context`, and HTTP experiment state keep
  surfacing the same persisted run identity after the transition.
- Updated MLflow docs, MCP/UI docs, tool descriptions, and the research
  workflow skill to tell agents to resume the plugin-created run.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.state.test_mlflow_tracking tests.surface.test_http_api.ResearchPluginHttpApiTest.test_running_transition_and_tool_hand_mlflow_block tests.surface.test_tool_contracts tests.surface.test_plugin_skills -v` (31 tests)
- `PYTHONPATH=. python -m unittest tests.workflow.test_experiment_slim tests.structure.test_plane_layout.PlaneImportLintTest -v` (31 tests)
- `PYTHONPATH=. python -m unittest tests.state.test_mlflow_tracking tests.state.test_store_migrations tests.surface.test_http_api tests.workflow.test_experiment_slim tests.surface.test_tool_contracts tests.surface.test_plugin_skills tests.structure.test_plane_layout.PlaneImportLintTest -v` (100 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (888 tests, 25 skipped)

## Batch 22: MLflow finalize/readback helper

Status: complete

Request addressed:

- Fix stale immediate MLflow readbacks.

Implementation notes:

- Added `mlflow.finalize_run` as a control-plane helper for the post-execution
  MLflow step. It defaults to the plugin-created run id persisted on the
  experiment.
- The helper can set a terminal MLflow status (`FINISHED`, `FAILED`, or
  `KILLED`) through the backend write URI, or run in readback-only mode with
  `status: null`.
- After the optional status update, it polls the MLflow REST `runs/get`
  endpoint briefly so an immediate stale `RUNNING` readback does not remain in
  plugin state.
- Successful readback refreshes the experiment's persisted `mlflow_run` block,
  so `experiment.get_state`, `mlflow.context`, and UI state surface the same
  terminal status.
- Updated central MLflow docs, MCP/UI docs, and the research workflow skill so
  agents call `mlflow.finalize_run` before submitting quantitative results.

Verification:

- `git diff --check`
- `python -m py_compile backend/mlflow/tracking.py backend/tools/tool_handlers.py backend/services/experiments.py backend/tools/contracts.py`
- `PYTHONPATH=. python -m unittest tests.state.test_mlflow_tracking tests.surface.test_http_api.ResearchPluginHttpApiTest.test_running_transition_and_tool_hand_mlflow_block tests.surface.test_tool_contracts tests.surface.test_plugin_skills -v` (34 tests)
- `PYTHONPATH=. python -m unittest tests.state.test_mlflow_tracking tests.state.test_store_migrations tests.surface.test_http_api tests.workflow.test_experiment_slim tests.surface.test_tool_contracts tests.surface.test_plugin_skills tests.structure.test_plane_layout.PlaneImportLintTest -v` (103 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (891 tests, 25 skipped)

## Batch 23: infrastructure retry while running

Status: complete

Request addressed:

- Better attempt retry semantics.

Implementation notes:

- Added `retry_running` as an explicit `experiment.transition` option available
  only from `running`.
- The transition is a same-status retry: the experiment remains `running`, the
  approved plan stays in force, and `attempt_index` is unchanged.
- Retry evidence is preserved in `revision_context` so future workflow guidance
  explains that execution is being rerun for infrastructure/interruption rather
  than because the design changed.
- `experiment.get_state.allowed_transitions` now advertises `retry_running`
  with its same-attempt precondition, and `workflow.status_and_next` keeps
  `experiment.transition` in the running execution gate's allowed actions.
- Updated MCP/UI/workflow docs and the research workflow skill so agents use
  this path when a sandbox dies or expires mid-run.

Verification:

- `git diff --check`
- `python -m py_compile backend/domain/workflow_gates.py backend/services/experiments.py backend/tools/contracts.py backend/services/workflow.py`
- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates.WorkflowGateTest.test_retry_running_keeps_current_attempt_and_records_infra_context tests.workflow.test_workflow_gates.WorkflowGateTest.test_retry_running_is_rejected_outside_running_status tests.workflow.test_workflow_gates.WorkflowGateTest.test_disallowed_transition_error_lists_allowed_options tests.surface.test_tool_contracts tests.surface.test_plugin_skills -v` (21 tests)
- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates tests.workflow.test_system_transitions tests.workflow.test_workflow_slim tests.surface.test_tool_contracts tests.surface.test_control_plane_contract tests.surface.test_http_api tests.surface.test_plugin_skills tests.structure.test_plane_layout.PlaneImportLintTest -v` (129 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (893 tests, 25 skipped)

## Batch 24: reflection gate checklist dashboard

Status: complete

Request addressed:

- Reflection is powerful but heavy.

Implementation notes:

- Added `gate_checklist` to reflection wave state, derived from the same
  synthesis gate table that enforces transitions.
- The checklist reports one item per missing/present roster lens during
  `reflecting`, validator-backed project graph / reflection document / change
  spec items during `synthesizing`, and pending/requested/started/passed
  `reflection_reviewer` state during `reflection_review`.
- Artifact checklist items run the same pinned-byte validators used by
  transitions, so invalid submitted graph/doc/spec artifacts show problems
  before `submit_reflection_artifacts` is attempted.
- Review checklist readiness matches the current snapshot id, preserving the
  existing snapshot pinning guarantees.
- Extended the reflection projection layer so tool-facing `reflection.get`
  shows external `submit_reflection_artifacts` and `reflection_review` names
  inside the checklist as well as in `allowed_transitions`.
- Updated MCP/UI/workflow docs and the project-reflection skill to tell agents
  to use the checklist as the guided publish dashboard.

Verification:

- `python -m py_compile backend/services/syntheses.py backend/domain/reflection_projection.py`
- `PYTHONPATH=. python -m unittest tests.workflow.test_reflection_projection tests.workflow.test_synthesis_gates.SynthesisGateTest.test_gate_checklist_tracks_missing_reflection_lenses tests.workflow.test_synthesis_gates.SynthesisGateTest.test_gate_checklist_tracks_reflection_artifacts tests.workflow.test_synthesis_gates.SynthesisGateTest.test_gate_checklist_tracks_reflection_review -v` (10 tests)
- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.workflow.test_synthesis_gates tests.workflow.test_reflection_projection tests.surface.test_tool_contracts tests.surface.test_plugin_skills -v` (80 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (897 tests, 25 skipped)

## Completion audit

Status: complete

Scope checked:

- `improvement_requests/2026-07-02_research_plugin_improvement_requests.md`
- Current tool contracts / app tool listing
- `plugin_improvement_progress.md`
- Git history through the sandbox cancellation reverts

Findings:

1. Make sandbox lifecycle more explicit and durable — canceled with Batch 15;
   pending a cleaner design.
2. Add a first-class project active VM concept — canceled with Batch 13;
   pending a cleaner design that does not let a new agent hijack another
   agent's sandbox.
3. Provide automatic lease extension or expiry warnings during active runs —
   canceled with Batch 14; pending a cleaner design.
4. Persist command status independently of SSH — addressed by Batch 19.
5. Automatic artifact retention from sandboxes — addressed by Batches 16 and 17.
6. Expose `workflow.status_and_next` consistently — verified as exposed in the
   current tool list (`workflow.status_and_next`) and covered by workflow,
   proxy, local-shipping, and contract tests.
7. Create local experiment folders when experiments materialize — addressed by
   Batches 5 and 18.
8. Batch resource association — addressed by Batch 2.
9. Protect `results.tsv` from clobbering — canceled as project-specific rather
   than a plugin-wide request.
10. Preflight linter for gated artifacts — addressed by Batch 3.
11. Better attempt retry semantics — addressed by Batch 23.
12. Auto-suggest claim updates after reviewed completion — addressed by Batch 12.
13. Create the MLflow run at experiment start — addressed by Batch 21.
14. Fix stale immediate MLflow readbacks — addressed by Batch 22.
15. Surface MLflow links directly in experiment state — addressed by Batch 10,
    extended by Batches 21 and 22.
16. Make storage easier to use end-to-end — addressed by Batch 1.
17. Associate storage objects with experiments and reports more visibly —
    addressed by Batch 7.
18. Give clearer guidance on what belongs in storage — addressed by Batch 8.
19. Reduce review boilerplate — addressed by Batch 11.
20. Show review gate checklist in experiment state — addressed by Batch 6.
21. Reviewer capability recovery — addressed by Batch 9.
22. Reflection is powerful but heavy — addressed by Batch 24.
23. Better graph diffing between reflections — addressed by Batch 20.
24. Materialized experiment visibility after reflection publish — addressed by
    Batch 18.

Verification:

- `PYTHONPATH=. python - <<'PY' ... app.list_tools() ...` confirmed
  `workflow.status_and_next` is present.
- Latest full suite after canceling the sandbox reuse/warning batches:
  `PYTHONPATH=. python -m unittest discover -s tests -v` (884 tests,
  25 skipped).
- Sandbox cancellation revert commits recorded: `a199771`, `118c0b1`, and
  `6e29684`.

Remaining scoped requests:

- The sandbox lifecycle, project-active VM, and expiry-warning requests are now
  intentionally unresolved pending a cleaner design. The two
  Git/reproducibility requests remain excluded by the saved-list scope.
- `Protect results.tsv from clobbering` is canceled as project-specific rather
  than a plugin-wide request.

## Batch 25: cancel generic results.tsv merge

Status: complete

Request addressed:

- Cancel Batch 4 / `results.merge_tsv`.

Implementation notes:

- Removed the generic `results.merge_tsv` tool contract, input schema, local
  handler, app facade, split-mode daemon route, and dataplane helper.
- Removed feature-specific merge tests and daemon smoke coverage for the
  canceled tool.
- Removed `results.tsv` from sandbox output auto-discovery defaults and docs so
  agents do not treat a project-specific ledger as a universal artifact.
- Updated the saved improvement request and completion audit to record the
  cancellation.

Verification:

- `python -m py_compile backend/app.py backend/composition/daemon_mode.py backend/tools/contracts.py backend/tools/tool_handlers.py backend/dataplane/sandbox_outputs.py`
- `PYTHONPATH=. python - <<'PY' ... app.list_tools() ...` confirmed
  `results.merge_tsv` is absent and no `results.*` tools remain.
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest.test_daemon_catalog_only_advertises_implemented_data_tools tests.sandbox.test_sandbox_outputs -v` (24 tests)
- `git diff --check`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (889 tests,
  25 skipped)

## Batch 26: cancel sandbox reuse and warning batches

Status: complete

Request addressed:

- Cancel commits `9bdd226`, `f86a0b1`, and `b8d8eb3`.

Implementation notes:

- Removed project-wide sandbox reuse as the default behavior for
  `sandbox.request`; it no longer scans for the newest live sandbox in the
  project and attaches it to a different experiment automatically.
- Removed `workflow.status_and_next` active-sandbox expiry warning generation
  and the corresponding MCP contract language.
- Removed sandbox lifecycle reason/detail surfaces and reason-specific terminal
  row details from the agent-facing sandbox views.
- Marked Batches 13, 14, and 15 as canceled and updated the completion audit to
  leave those UX requests pending a cleaner design.

Verification:

- `rg -n 'project_active_sandbox|list_running_project_rows|_project_reuse_candidate|sandbox_expiry|SANDBOX_EXPIRY_WARNING_SECONDS|_with_sandbox_expiry_warning|lifecycle_reason|lifecycle_detail|terminal_reason' backend docs tests plugin_improvement_progress.md` found only the cancellation notes in this file.
- `git diff --check`
- `python -m py_compile backend/services/sandbox/sandboxes.py backend/services/sandbox/sandbox_registry.py backend/services/sandbox/sandbox_views.py backend/services/sandbox/sandbox_daemons.py backend/services/sandbox/sandbox_provisioner.py backend/services/workflow.py backend/tools/contracts.py`
- `PYTHONPATH=. python -m unittest tests.sandbox.test_sandbox_service tests.sandbox.test_sandbox_heartbeat tests.sandbox.test_sandbox_identity tests.workflow.test_workflow_gates tests.surface.test_tool_contracts -v` (128 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (884 tests, 25 skipped)
