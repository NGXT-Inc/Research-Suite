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
