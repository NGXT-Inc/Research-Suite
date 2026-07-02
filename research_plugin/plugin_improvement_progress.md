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
