from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.contracts import (
    AGGREGATE_TOOL_NAMES,
    CONTROL_PLANE_TOOL_NAMES,
    DATA_PLANE_TOOL_NAMES,
    TOOL_CONTRACTS,
    static_tool_catalog,
)
from backend.execution.backends.fake import FakeSandboxBackend
from backend.project_router import ProjectRouter
from backend.tool_facade import ToolDispatcher
from backend.tool_handlers import build_control_tool_handlers, build_local_tool_handlers


class _HandlerTarget:
    def __getattr__(self, _name: str):
        def _handler(**_kwargs):
            return {}

        return _handler


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
        "reviews": target,
        "sandboxes": target,
        "feed": target,
    }


class ToolContractRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def test_registered_tools_match_contracts_and_have_descriptions(self) -> None:
        tools = {tool["name"]: tool for tool in self.app.list_tools()}

        self.assertEqual(set(tools), set(TOOL_CONTRACTS))
        for name, contract in TOOL_CONTRACTS.items():
            self.assertTrue(contract.description.strip(), name)
            self.assertEqual(tools[name]["description"], contract.description)

    def test_static_catalog_matches_app_list_tools(self) -> None:
        # The static catalog is what the router serves without instantiating an
        # app; it must be indistinguishable from a live app's listing.
        self.assertEqual(static_tool_catalog(), self.app.list_tools())

    def test_sandbox_tool_descriptions_carry_lifecycle_guidance(self) -> None:
        tools = {tool["name"]: tool for tool in self.app.list_tools()}
        self.assertIn("MLflow/TensorBoard", tools["sandbox.request"]["description"])
        self.assertIn("expiry", tools["sandbox.get"]["description"])
        self.assertIn("poll provisioning", tools["sandbox.get"]["description"])
        self.assertIn("sandbox.sync before release", tools["sandbox.release"]["description"])
        self.assertIn("hosted control", tools["sandbox.release"]["description"])
        self.assertIn("metrics snapshot", tools["sandbox.release"]["description"])


class StaticCatalogNoSideEffectTest(unittest.TestCase):
    def test_router_tool_listing_creates_no_template_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.sqlite"
            router = ProjectRouter(registry_db_path=registry)
            try:
                tools = router.list_tools()
            finally:
                router.shutdown()
            self.assertEqual({tool["name"] for tool in tools}, set(TOOL_CONTRACTS))
            self.assertFalse((Path(tmp) / "_tool_schema").exists())


class ToolDispatcherTest(unittest.TestCase):
    def test_dispatcher_can_expose_a_control_subset(self) -> None:
        tool_names = CONTROL_PLANE_TOOL_NAMES | AGGREGATE_TOOL_NAMES
        handlers = {name: (lambda **_: {}) for name in tool_names}
        dispatcher = ToolDispatcher(
            handlers=handlers,
            permissions=object(),
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
        )

        self.assertEqual(set(handlers), set(TOOL_CONTRACTS))

    def test_control_handlers_exclude_data_plane_tools(self) -> None:
        handlers = build_control_tool_handlers(**_handler_targets())

        self.assertEqual(set(handlers), CONTROL_PLANE_TOOL_NAMES | AGGREGATE_TOOL_NAMES)
        self.assertFalse(set(handlers) & DATA_PLANE_TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
