from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.contracts import TOOL_CONTRACTS
from backend.execution.backends.fake import FakeSandboxBackend


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


if __name__ == "__main__":
    unittest.main()
