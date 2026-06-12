"""Per-client adapter manifests stay consistent with the shared content tree.

One canonical tree (bin/, skills/, agents/) is exposed to five clients through
thin adapters. These tests pin the adapter contracts: every manifest parses,
points at a launcher that exists, supplies the project root the way that
client needs it, and carries the package version. See docs/CLIENTS.md.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from backend import __version__ as BACKEND_VERSION
from tests.paths import PLUGIN_ROOT


def _executable(path: Path) -> bool:
    return path.exists() and os.access(path, os.X_OK)


class CursorAdapterTest(unittest.TestCase):
    def test_plugin_manifest(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / ".cursor-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "research-plugin")
        self.assertEqual(manifest["version"], BACKEND_VERSION)

    def test_mcp_config_supplies_workspace_root(self) -> None:
        # Cursor does not spawn stdio servers in the workspace, so the repo
        # root must arrive via ${workspaceFolder}, never via cwd.
        config = json.loads((PLUGIN_ROOT / "mcp.json").read_text())
        server = config["mcpServers"]["research-plugin"]
        self.assertEqual(server["env"]["RESEARCH_PLUGIN_REPO_ROOT"], "${workspaceFolder}")
        command = Path(server["command"])
        self.assertFalse(command.is_absolute(), "Cursor bundle must stay install-relative")
        self.assertTrue(_executable(PLUGIN_ROOT / command))


class GeminiAdapterTest(unittest.TestCase):
    def test_extension_manifest(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "gemini-extension.json").read_text())
        self.assertEqual(manifest["name"], "research-plugin")
        self.assertEqual(manifest["version"], BACKEND_VERSION)
        self.assertTrue((PLUGIN_ROOT / manifest["contextFileName"]).is_file())

        server = manifest["mcpServers"]["research-plugin"]
        self.assertEqual(server["env"]["RESEARCH_PLUGIN_REPO_ROOT"], "${workspacePath}")
        command = server["command"]
        self.assertTrue(command.startswith("${extensionPath}"))
        resolved = command.replace("${extensionPath}", str(PLUGIN_ROOT)).replace("${/}", os.sep)
        self.assertTrue(_executable(Path(resolved)))


class OpenCodeAdapterTest(unittest.TestCase):
    def test_installer_and_config_example(self) -> None:
        adapter = PLUGIN_ROOT / "clients" / "opencode"
        self.assertTrue(_executable(adapter / "install.sh"))

        config = json.loads((adapter / "opencode.json.example").read_text())
        server = config["mcp"]["research-plugin"]
        self.assertEqual(server["type"], "local")
        self.assertEqual(Path(server["command"][0]).name, "research-plugin-mcp")

    def test_reviewer_agents_load_matching_review_skills(self) -> None:
        # OpenCode agents are thin wrappers: each must defer to the same-named
        # skill so the operating procedure is never duplicated per client.
        for agent_path in sorted((PLUGIN_ROOT / "clients" / "opencode" / "agents").glob("*.md")):
            skill_name = agent_path.stem
            self.assertTrue((PLUGIN_ROOT / "skills" / skill_name / "SKILL.md").is_file())
            self.assertIn(f"`{skill_name}` skill", agent_path.read_text())


if __name__ == "__main__":
    unittest.main()
