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

Status: complete

Request addressed:

- Protect `results.tsv` from clobbering.

Implementation notes:

- Added `results.merge_tsv` as a data-plane helper for merging a sandbox
  produced TSV into a canonical local results ledger.
- The merge parses TSV structurally, requires stable key columns or infers a
  common row id column, skips identical duplicate rows, atomically appends new
  rows, and refuses conflicting rows before changing the target file.
- Wired the tool through local mode and split-mode daemon routing, with docs
  updates for the MCP contract and control/data-plane split.

Verification:

- `PYTHONPATH=. python -m unittest tests.sandbox.test_results_tsv_merge -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_results_merge_tool -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_tool_contracts tests.structure.test_plane_layout.ToolPlanePartitionTest -v`
- `PYTHONPATH=. python -m unittest tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest.test_results_merge_tsv_updates_local_ledger_without_control_mutation tests.surface.test_split_mode_smoke.DaemonResourceForwardingTest.test_daemon_catalog_only_advertises_implemented_data_tools -v`
- `PYTHONPATH=. python -m unittest discover -s tests -v` (862 tests, 25 skipped)

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

Status: complete

Request addressed:

- Add a first-class project active VM concept so agents keep the same VM across
  experiments by default.

Implementation notes:

- `sandbox.request` now searches the project for the newest confirmed-live
  sandbox before provisioning another VM, attaches it to the requested
  experiment, and returns `reuse_source: "project_active_sandbox"`.
- `additional=true` remains the explicit escape hatch for a parallel sandbox,
  and tests that intentionally need separate VMs now request that explicitly.
- Split-mode daemon requests no longer let their provisional generated
  `sandbox_uid` accidentally force a new VM when a reusable project sandbox
  already exists.
- Hardware selection menus for Lambda Labs and Thunder Compute are skipped when
  a project sandbox can be reused, preserving the incumbent instance type.
- Updated MCP/tool/backend docs to describe the project-level reuse default.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.sandbox.test_sandbox_heartbeat.SandboxHeartbeatMonitorTest.test_idle_sandbox_is_reaped_while_busy_sandbox_is_spared tests.sandbox.test_sandbox_identity.SandboxIdentityTest.test_sandbox_uid_is_unique_and_stable_across_upserts tests.sandbox.test_sandbox_service.SandboxServiceTest.test_request_reuses_project_live_sandbox_for_new_experiment tests.sandbox.test_sandbox_service.SandboxServiceTest.test_request_additional_bypasses_project_live_sandbox_reuse tests.sandbox.test_sandbox_service.SandboxServiceTest.test_data_plane_request_reuses_project_live_sandbox_despite_provisional_uid tests.sandbox.test_sandbox_service.SandboxServiceTest.test_standalone_request_reuses_project_live_sandbox -v` (6 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (877 tests, 25 skipped)

## Batch 14: active sandbox expiry warnings

Status: complete

Request addressed:

- Provide expiry warnings during active runs.

Implementation notes:

- `workflow.status_and_next` now adds a `workflow.warnings[]` entry for
  `running` experiments whose live sandbox expires within one hour.
- The warning identifies the sandbox, expiry time, seconds remaining, severity,
  and concrete retention-oriented next actions.
- Expired sandboxes report a critical warning that tells the agent to reconcile
  with `sandbox.get`, retain reachable outputs immediately, and request a
  replacement if needed.
- Kept this advisory rather than provider-specific auto-extension; no sandbox
  lifecycle mutation or provider call is performed during workflow polling.
- Documented the optional warning shape in the MCP contract.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.workflow.test_workflow_gates.WorkflowGateTest.test_running_workflow_warns_when_live_sandbox_expiry_is_close tests.workflow.test_workflow_slim.WorkflowSlimTest.test_active_sandbox_is_summarized tests.surface.test_tool_contracts tests.structure.test_plane_layout.PlaneImportLintTest -v` (42 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (878 tests, 25 skipped)

## Batch 15: sandbox lifecycle reason surface

Status: complete

Request addressed:

- Make sandbox lifecycle more explicit and durable.

Implementation notes:

- Terminal sandbox paths now persist a reason code in the sandbox row's
  terminal detail: `user_release`, `expired`, `idle_timeout`, or
  `provider_unreachable`.
- Shared sandbox projections now expose `lifecycle_reason` and
  `lifecycle_detail` across `sandbox.request`, `sandbox.get`, `sandbox.list`,
  workflow sandbox summaries, and HTTP/UI sandbox views.
- Provider liveness reconciliation now emits `sandbox.terminated` with
  `reason: "provider_unreachable"` instead of labeling that case as expiry.
- Failed provisioning rows derive `provisioning_failed` or
  `provisioning_interrupted` from the existing error text.
- Updated the MCP contract and `sandbox.get` tool description to make the
  lifecycle reason field discoverable.

Verification:

- `git diff --check`
- `PYTHONPATH=. python -m unittest tests.sandbox.test_sandbox_service.SandboxServiceTest.test_reaper_does_not_change_experiment_status tests.sandbox.test_sandbox_service.SandboxServiceTest.test_get_reconciles_dead_sandbox tests.sandbox.test_sandbox_service.SandboxServiceTest.test_release_terminates tests.sandbox.test_sandbox_heartbeat.SandboxHeartbeatMonitorTest.test_idle_sandbox_is_reaped_while_busy_sandbox_is_spared tests.surface.test_tool_contracts tests.structure.test_plane_layout.PlaneImportLintTest -v` (44 tests)
- `PYTHONPATH=. python -m unittest discover -s tests -v` (878 tests, 25 skipped)

## Batch 16: sandbox output pull helper

Status: complete

Request addressed:

- Automatic artifact retention from sandboxes.

Implementation notes:

- Added `sandbox.pull_outputs` as a data-plane helper that copies selected
  files or directories from a running sandbox's remote `experiment_dir` into
  the local experiment folder over SSH/rsync.
- With no explicit paths, the tool checks for common retained outputs:
  `results/`, `figures/`, `report.md`, `graph.json`, `metrics.json`,
  `results.json`, and `results.tsv`, then pulls only the paths that exist.
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
