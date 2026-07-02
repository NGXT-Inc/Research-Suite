from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from backend.app import ResearchPluginApp
from backend.config import STORAGE_PROVIDER_ENV_VAR
from backend.tools.contracts import (
    AGGREGATE_TOOL_NAMES,
    CONTROL_PLANE_TOOL_NAMES,
    DATA_PLANE_TOOL_NAMES,
    ExperimentMaterializeFoldersInput,
    MlflowFinalizeRunInput,
    ResourceAssociateBatchInput,
    ResourceValidateInput,
    ReviewRequestAndStartInput,
    SandboxPullOutputsInput,
    StorageCompleteUploadInput,
    StorageDownloadFileInput,
    StorageListInput,
    StorageObjectInput,
    StoragePutObjectInput,
    StorageResolveInput,
    StorageUploadFileInput,
    STORAGE_TOOL_NAMES,
    TOOL_CONTRACTS,
    available_tool_names,
    static_tool_catalog,
)
from backend.execution.backends.fake import FakeSandboxBackend
from backend.daemon.project_router import ProjectRouter
from backend.tools.tool_facade import ToolDispatcher
from backend.tools.tool_handlers import build_control_tool_handlers, build_local_tool_handlers


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


def _handler_targets() -> dict[str, _HandlerTarget]:
    target = _HandlerTarget()
    return {
        "workflow": target,
        "projects": target,
        "project_overview": target,
        "claims": target,
        "experiments": target,
        "reflections": target,
        "resources": target,
        "storage": target,
        "reviews": target,
        "sandboxes": target,
        "mlflow_tracking": target,
        "feed": target,
    }


class ToolContractRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.env_patch = patch.dict(os.environ, {STORAGE_PROVIDER_ENV_VAR: ""})
        self.env_patch.start()
        self.app = ResearchPluginApp(
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

    def test_static_catalog_matches_app_list_tools(self) -> None:
        # The static catalog is what the router serves without instantiating an
        # app; it must be indistinguishable from a live app's listing.
        self.assertEqual(static_tool_catalog(), self.app.list_tools())

    def test_sandbox_tool_descriptions_carry_lifecycle_guidance(self) -> None:
        tools = {tool["name"]: tool for tool in self.app.list_tools()}
        self.assertNotIn("MLflow", tools["sandbox.request"]["description"])
        self.assertNotIn("TensorBoard", tools["sandbox.request"]["description"])
        self.assertIn("durable storage", tools["sandbox.request"]["description"])
        self.assertIn("expiry", tools["sandbox.get"]["description"])
        self.assertIn("lifecycle_reason", tools["sandbox.get"]["description"])
        self.assertIn("poll provisioning", tools["sandbox.get"]["description"])
        self.assertIn("confirm_retained", tools["sandbox.release"]["description"])
        self.assertIn("retention checklist", tools["sandbox.release"]["description"])
        self.assertIn("metrics snapshot", tools["sandbox.release"]["description"])
        self.assertIn("local experiment folder", tools["sandbox.pull_outputs"]["description"])
        self.assertIn("sandbox.release", tools["sandbox.pull_outputs"]["description"])

    def test_storage_tools_registered_with_expected_input_models(self) -> None:
        expected = {
            "storage.put_object": (StoragePutObjectInput, "control"),
            "storage.upload_file": (StorageUploadFileInput, "data"),
            "storage.complete_upload": (StorageCompleteUploadInput, "control"),
            "storage.list": (StorageListInput, "control"),
            "storage.resolve": (StorageResolveInput, "control"),
            "storage.download_file": (StorageDownloadFileInput, "data"),
            "storage.pin": (StorageObjectInput, "control"),
            "storage.unpin": (StorageObjectInput, "control"),
            "storage.renew": (StorageObjectInput, "control"),
            "storage.delete": (StorageObjectInput, "control"),
        }
        for name, (model, plane) in expected.items():
            self.assertIs(TOOL_CONTRACTS[name].input_model, model)
            self.assertEqual(TOOL_CONTRACTS[name].plane, plane)
        self.assertIn("checkpoints/models", TOOL_CONTRACTS["storage.put_object"].description)
        self.assertIn("logs/traces over about 10 MB", TOOL_CONTRACTS["storage.upload_file"].description)

    def test_resource_batch_association_is_data_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["resource.associate_batch"].input_model,
            ResourceAssociateBatchInput,
        )
        self.assertEqual(TOOL_CONTRACTS["resource.associate_batch"].plane, "data")

    def test_resource_validate_is_data_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["resource.validate"].input_model,
            ResourceValidateInput,
        )
        self.assertEqual(TOOL_CONTRACTS["resource.validate"].plane, "data")

    def test_sandbox_pull_outputs_is_data_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["sandbox.pull_outputs"].input_model,
            SandboxPullOutputsInput,
        )
        self.assertEqual(TOOL_CONTRACTS["sandbox.pull_outputs"].plane, "data")

    def test_experiment_materialize_folders_is_data_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["experiment.materialize_folders"].input_model,
            ExperimentMaterializeFoldersInput,
        )
        self.assertEqual(
            TOOL_CONTRACTS["experiment.materialize_folders"].plane,
            "data",
        )

    def test_review_request_and_start_is_control_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["review.request_and_start"].input_model,
            ReviewRequestAndStartInput,
        )
        self.assertEqual(TOOL_CONTRACTS["review.request_and_start"].plane, "control")

    def test_mlflow_finalize_run_is_control_plane(self) -> None:
        self.assertIs(
            TOOL_CONTRACTS["mlflow.finalize_run"].input_model,
            MlflowFinalizeRunInput,
        )
        self.assertEqual(TOOL_CONTRACTS["mlflow.finalize_run"].plane, "control")


class StaticCatalogNoSideEffectTest(unittest.TestCase):
    def test_router_tool_listing_creates_no_template_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.sqlite"
            with patch.dict(os.environ, {STORAGE_PROVIDER_ENV_VAR: ""}):
                router = ProjectRouter(registry_db_path=registry)
                try:
                    tools = router.list_tools()
                finally:
                    router.shutdown()
            self.assertEqual(
                {tool["name"] for tool in tools},
                available_tool_names(storage_enabled=False),
            )
            self.assertFalse((Path(tmp) / "_tool_schema").exists())


class ToolDispatcherTest(unittest.TestCase):
    def test_dispatcher_can_expose_a_control_subset(self) -> None:
        tool_names = CONTROL_PLANE_TOOL_NAMES | AGGREGATE_TOOL_NAMES
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
    def test_local_handlers_cover_every_contract(self) -> None:
        target = _HandlerTarget()
        handlers = build_local_tool_handlers(
            **_handler_targets(),
            resource_register_file=target.register_file,
            resource_validate=target.validate,
            experiment_materialize_folders=target.materialize_folders,
        )

        self.assertEqual(set(handlers), set(TOOL_CONTRACTS))

    def test_control_handlers_exclude_data_plane_tools(self) -> None:
        handlers = build_control_tool_handlers(**_handler_targets())

        self.assertEqual(set(handlers), CONTROL_PLANE_TOOL_NAMES | AGGREGATE_TOOL_NAMES)
        self.assertFalse(set(handlers) & DATA_PLANE_TOOL_NAMES)

    def test_control_handlers_omit_storage_when_disabled(self) -> None:
        targets = _handler_targets()
        targets["storage"] = None
        handlers = build_control_tool_handlers(**targets)

        self.assertFalse(set(handlers) & STORAGE_TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
