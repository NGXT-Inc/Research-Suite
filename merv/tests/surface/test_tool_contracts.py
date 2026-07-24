from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError as PydanticValidationError

from tests.support.brain import TestBrain
from merv.brain.surface.config import STORAGE_PROVIDER_ENV_VAR
from merv.brain.surface.tools.contracts import (
    CONTROL_PLANE_TOOL_NAMES,
    DATA_PLANE_TOOL_NAMES,
    MCP_HIDDEN_TOOL_NAMES,
    ArtifactFindInput,
    ArtifactSubmitInput,
    MlflowFinalizeRunInput,
    SandboxExtendInput,
    SandboxPullOutputsInput,
    SandboxRequestInput,
    StorageCompleteUploadInput,
    StorageFetchInput,
    StorageFindInput,
    StorageObjectInput,
    StoragePutObjectInput,
    StorageSubmitInput,
    STORAGE_TOOL_NAMES,
    TOOL_CONTRACTS,
    TOOL_MANIFEST,
    TOOL_PLANE_REGISTRY,
    available_tool_names,
    static_tool_catalog,
    tool_plane,
)
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.surface.tools.tool_facade import ToolDispatcher
from merv.brain.surface.tools.tool_handlers import build_control_tool_handlers
from merv.proxy.local_data_plane import LocalDataPlane


class _HandlerTarget:
    def __getattr__(self, _name: str):
        def _handler(**_kwargs):
            return {}

        return _handler


class _PermissionTarget:
    def reject_reviewer_mutation(
        self, *, tool_name: str, review_session_id: str | None
    ) -> None:
        return None


def _handler_targets() -> dict[str, Any]:
    target = _HandlerTarget()
    return {
        "workflow": target,
        "projects": target,
        "claims": target,
        "create_experiment": target,
        "agent_experiment": target,
        "reflection_tools": target,
        "artifact_submissions": target,
        "storage": target,
        "reviews": target,
        "sandboxes": target,
        "feed": target,
        "experiment_transition": target,
        "experiment_exhibit": target,
        "tracking_context": target,
        "tracking_finalize": target,
        "review_status": target,
        "operations": target,
        "litreview": target,
    }


class ToolContractRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.env_patch = patch.dict(os.environ, {STORAGE_PROVIDER_ENV_VAR: ""})
        self.env_patch.start()
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )

    def tearDown(self) -> None:
        self.app.shutdown()
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_registered_tools_match_contracts_and_have_descriptions(self) -> None:
        tools = {tool["name"]: tool for tool in self.app.list_tools()}

        self.assertEqual(set(tools), available_tool_names(storage_enabled=False))
        self.assertFalse(set(tools) & STORAGE_TOOL_NAMES)
        for name, contract in TOOL_CONTRACTS.items():
            if name not in tools:
                continue
            self.assertTrue(contract.description.strip(), name)
            self.assertEqual(tools[name]["description"], contract.description)

    def test_live_local_split_composes_control_and_proxy_data_planes(self) -> None:
        control = set(self.app._app.tools._tools)
        available = available_tool_names(storage_enabled=False)
        data = DATA_PLANE_TOOL_NAMES & available
        self.assertEqual(control, CONTROL_PLANE_TOOL_NAMES & available)
        self.assertIsInstance(self.app._data_plane, LocalDataPlane)
        self.assertEqual(control | data, available)
        self.assertFalse(control & data)

    def test_static_catalog_matches_app_list_tools(self) -> None:
        # The static catalog is what the router serves without instantiating an
        # app; it must be indistinguishable from a live app's listing.
        self.assertEqual(static_tool_catalog(), self.app.list_tools())

    def test_plane_registry_classifies_every_tool(self) -> None:
        self.assertEqual(set(TOOL_PLANE_REGISTRY), set(TOOL_CONTRACTS))
        self.assertLessEqual(set(TOOL_PLANE_REGISTRY.values()), {"control", "data"})

    def test_manifest_owns_all_routing_and_handler_metadata(self) -> None:
        self.assertIs(TOOL_CONTRACTS, TOOL_MANIFEST)
        for name, tool in TOOL_MANIFEST.items():
            self.assertIn(tool.visibility, {"public", "internal"}, name)
            self.assertIn(
                tool.scope_strategy,
                {"linked-project", "caller-selected", "capability", "none"},
                name,
            )
            self.assertIn(
                tool.execution_strategy,
                {"control", "local", "control-plus-local-enrichment", "local-orchestration"},
                name,
            )
            self.assertTrue(tool.handler_identity, name)
            self.assertLessEqual(set(tool.feature_requirements), {"storage"}, name)
        self.assertEqual(TOOL_MANIFEST["project"].execution_strategy, "local-orchestration")
        self.assertEqual(TOOL_MANIFEST["project"].plane, "control")

    def test_hidden_tools_stay_in_catalog_with_hidden_flag(self) -> None:
        # UI/proxy-internal tools remain dispatchable and keep their catalog
        # entry (the proxy routes off plane/schema) but carry hidden=True so
        # the proxy's tools/list drops them from the agent surface.
        self.assertLessEqual(MCP_HIDDEN_TOOL_NAMES, set(TOOL_CONTRACTS))
        self.assertIn("project.get", MCP_HIDDEN_TOOL_NAMES)
        self.assertIn("project.update", MCP_HIDDEN_TOOL_NAMES)
        # review.status is served for REST/UI reads and internal dispatch, but
        # agents poll workflow.status_and_next (its review_gate re-reports state).
        self.assertIn("review.status", MCP_HIDDEN_TOOL_NAMES)
        # Enumeration readers embedded in other responses stay REST/UI-only.
        # sandbox.list is NO LONGER hidden: a project-scoped mk_ key needs it to
        # enumerate the project's (shared) sandboxes over MCP (no-dataplane
        # Phase C).
        for reader in (
            "claim.list",
            "experiment.list",
            "reflection.list",
            "sandbox.health",
        ):
            self.assertIn(reader, MCP_HIDDEN_TOOL_NAMES, reader)
        self.assertNotIn("sandbox.list", MCP_HIDDEN_TOOL_NAMES)
        # storage_enabled=True so the hidden storage primitives appear in the
        # catalog (setUp clears the storage provider env var).
        catalog = {tool["name"]: tool for tool in static_tool_catalog(storage_enabled=True)}
        for name in MCP_HIDDEN_TOOL_NAMES:
            self.assertTrue(catalog[name].get("hidden"), name)
        for name, tool in catalog.items():
            if name not in MCP_HIDDEN_TOOL_NAMES:
                self.assertNotIn("hidden", tool, name)

    def test_sandbox_tool_descriptions_carry_lifecycle_guidance(self) -> None:
        tools = {tool["name"]: tool for tool in self.app.list_tools()}
        self.assertNotIn("MLflow", tools["sandbox.request"]["description"])
        self.assertNotIn("TensorBoard", tools["sandbox.request"]["description"])
        self.assertIn("durable storage", tools["sandbox.request"]["description"])
        self.assertIn("public_key", tools["sandbox.request"]["description"])
        self.assertIn("public_key_source", tools["sandbox.request"]["description"])
        self.assertIn("expiry", tools["sandbox.get"]["description"])
        self.assertIn("poll provisioning", tools["sandbox.get"]["description"])
        self.assertIn("public_key_source", tools["sandbox.get"]["description"])
        self.assertIn("confirm_retained", tools["sandbox.release"]["description"])
        self.assertIn("retention checklist", tools["sandbox.release"]["description"])
        self.assertIn("metrics snapshot", tools["sandbox.release"]["description"])
        self.assertIn("local experiment folder", tools["sandbox.pull_outputs"]["description"])
        self.assertIn("object storage", tools["sandbox.pull_outputs"]["description"])
        self.assertIn("sandbox.release", tools["sandbox.pull_outputs"]["description"])

    def test_storage_tools_registered_with_expected_input_models(self) -> None:
        expected = {
            "storage.put_object": (StoragePutObjectInput, "control"),
            "storage.submit": (StorageSubmitInput, "control"),
            "storage.complete_upload": (StorageCompleteUploadInput, "control"),
            "storage.fetch": (StorageFetchInput, "control"),
            "storage.find": (StorageFindInput, "control"),
            "storage.object": (StorageObjectInput, "control"),
        }
        self.assertEqual(
            STORAGE_TOOL_NAMES, set(expected), "storage surface must be exactly these 6 tools"
        )
        for name, (model, plane) in expected.items():
            self.assertIs(TOOL_CONTRACTS[name].input_model, model)
            self.assertEqual(tool_plane(name), plane)
        # The removed tools must be gone from the registry entirely.
        for removed in (
            "storage.list",
            "storage.resolve",
            "storage.pin",
            "storage.unpin",
            "storage.renew",
            "storage.delete",
        ):
            self.assertNotIn(removed, TOOL_CONTRACTS)
        self.assertIn("checkpoints/models", TOOL_CONTRACTS["storage.put_object"].description)
        self.assertIn("logs/traces over about 10 MB", TOOL_CONTRACTS["storage.submit"].description)

    def test_storage_find_enforces_resolve_vs_list_mode(self) -> None:
        # List mode: neither selector.
        StorageFindInput.model_validate({"project_id": "p", "kind": "model"})
        # Resolve mode: exactly one selector.
        StorageFindInput.model_validate({"project_id": "p", "object_id": "so_1"})
        StorageFindInput.model_validate({"project_id": "p", "name": "datasets/x"})
        # Both selectors is ambiguous.
        with self.assertRaises(PydanticValidationError):
            StorageFindInput.model_validate(
                {"project_id": "p", "object_id": "so_1", "name": "datasets/x"}
            )
        # version without a resolve target is meaningless.
        with self.assertRaises(PydanticValidationError):
            StorageFindInput.model_validate({"project_id": "p", "version": 2})

    def test_storage_object_action_is_required_and_enumerated(self) -> None:
        StorageObjectInput.model_validate(
            {"project_id": "p", "object_id": "so_1", "action": "pin"}
        )
        with self.assertRaises(PydanticValidationError):
            StorageObjectInput.model_validate({"project_id": "p", "object_id": "so_1"})
        with self.assertRaises(PydanticValidationError):
            StorageObjectInput.model_validate(
                {"project_id": "p", "object_id": "so_1", "action": "purge"}
            )

    def test_hidden_storage_primitives_stay_dispatchable_but_unadvertised(self) -> None:
        for name in ("storage.put_object", "storage.complete_upload"):
            self.assertIn(name, MCP_HIDDEN_TOOL_NAMES, name)
            self.assertIn(name, STORAGE_TOOL_NAMES, name)
            self.assertIn(name, TOOL_CONTRACTS, name)
        catalog = {tool["name"]: tool for tool in static_tool_catalog(storage_enabled=True)}
        self.assertTrue(catalog["storage.put_object"].get("hidden"))
        self.assertTrue(catalog["storage.complete_upload"].get("hidden"))
        # The merged tools stay visible.
        self.assertNotIn("hidden", catalog["storage.find"])
        self.assertNotIn("hidden", catalog["storage.object"])

    def test_artifact_tools_are_control_plane(self) -> None:
        self.assertIs(TOOL_CONTRACTS["artifact.submit"].input_model, ArtifactSubmitInput)
        self.assertIs(TOOL_CONTRACTS["artifact.find"].input_model, ArtifactFindInput)
        self.assertEqual(tool_plane("artifact.submit"), "control")
        self.assertEqual(tool_plane("artifact.find"), "control")
        # The whole resource-tracking tool family died with the resource cut.
        for removed in ("resource.register", "resource.find", "resource.delete"):
            self.assertNotIn(removed, TOOL_CONTRACTS)

    def test_artifact_submit_requires_lens_id_only_for_lens_docs(self) -> None:
        base = {
            "project_id": "p",
            "target_type": "reflection",
            "target_id": "syn_1",
            "path": "reflections/amplify.md",
        }
        with self.assertRaises(PydanticValidationError):
            ArtifactSubmitInput.model_validate({**base, "role": "reflection_lens_doc"})
        with self.assertRaises(PydanticValidationError):
            ArtifactSubmitInput.model_validate(
                {**base, "role": "reflection_doc", "lens_id": "amplify"}
            )
        parsed = ArtifactSubmitInput.model_validate(
            {**base, "role": "reflection_lens_doc", "lens_id": "amplify"}
        )
        self.assertEqual(parsed.lens_id, "amplify")

    def test_sandbox_pull_outputs_is_data_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["sandbox.pull_outputs"].input_model,
            SandboxPullOutputsInput,
        )
        self.assertEqual(tool_plane("sandbox.pull_outputs"), "data")

    def test_sandbox_request_accepts_caller_public_key(self) -> None:
        parsed = SandboxRequestInput.model_validate(
            {
                "project_id": "proj_1",
                "public_key": "ssh-ed25519 " + ("A" * 48) + " caller@test",
            }
        )

        self.assertTrue(parsed.public_key.startswith("ssh-ed25519 "))

    def test_sandbox_request_rejects_private_or_multiline_key_material(self) -> None:
        for public_key in (
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "ssh-ed25519 " + ("A" * 48) + "\ncomment",
            "not-a-key " + ("A" * 48),
        ):
            with self.subTest(public_key=public_key):
                with self.assertRaises(PydanticValidationError):
                    SandboxRequestInput.model_validate(
                        {"project_id": "proj_1", "public_key": public_key}
                    )

    def test_sandbox_extend_is_control_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["sandbox.extend"].input_model,
            SandboxExtendInput,
        )
        self.assertEqual(tool_plane("sandbox.extend"), "control")

    def test_experiment_materialize_folders_is_deleted(self) -> None:
        # D6: folder layout is now a skill instruction, not a tool.
        self.assertNotIn("experiment.materialize_folders", TOOL_CONTRACTS)

    def test_review_request_and_start_is_removed(self) -> None:
        # Removed: it started the reviewer session server-side, letting the
        # producer submit against its own gate. review.request's spawn-ready
        # handoff is the sanctioned one-call path.
        self.assertNotIn("review.request_and_start", TOOL_CONTRACTS)

    def test_mlflow_finalize_run_is_control_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["mlflow.finalize_run"].input_model,
            MlflowFinalizeRunInput,
        )
        self.assertEqual(tool_plane("mlflow.finalize_run"), "control")


class StaticCatalogNoSideEffectTest(unittest.TestCase):
    def test_static_tool_listing_creates_no_template_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {STORAGE_PROVIDER_ENV_VAR: ""}):
                tools = static_tool_catalog(storage_enabled=False)
            self.assertEqual(
                {tool["name"] for tool in tools},
                available_tool_names(storage_enabled=False),
            )
            self.assertFalse((Path(tmp) / "_tool_schema").exists())


class ToolDispatcherTest(unittest.TestCase):
    def test_dispatcher_can_expose_a_control_subset(self) -> None:
        tool_names = CONTROL_PLANE_TOOL_NAMES
        handlers = {name: (lambda **_: {}) for name in tool_names}
        dispatcher = ToolDispatcher(
            handlers=handlers,
            permissions=_PermissionTarget(),
            activity=object(),
            tool_calls=object(),
            tool_names=tool_names,
        )

        listed_names = {tool["name"] for tool in dispatcher.list_tools()}
        self.assertEqual(listed_names, tool_names)
        self.assertFalse(listed_names & DATA_PLANE_TOOL_NAMES)


class ToolHandlerRegistryTest(unittest.TestCase):
    def test_control_handlers_exclude_data_plane_tools(self) -> None:
        handlers = build_control_tool_handlers(**_handler_targets())

        self.assertEqual(set(handlers), CONTROL_PLANE_TOOL_NAMES)
        self.assertFalse(set(handlers) & DATA_PLANE_TOOL_NAMES)

    def test_control_handlers_omit_storage_when_disabled(self) -> None:
        targets = _handler_targets()
        targets["storage"] = None
        handlers = build_control_tool_handlers(**targets)

        self.assertFalse(set(handlers) & STORAGE_TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
