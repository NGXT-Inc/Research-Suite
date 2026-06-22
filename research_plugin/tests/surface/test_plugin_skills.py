from __future__ import annotations

import shutil
import subprocess
import unittest

from tests.paths import PLUGIN_ROOT


class PluginSkillTest(unittest.TestCase):
    def test_skill_frontmatter_is_valid_yaml(self) -> None:
        ruby = shutil.which("ruby")
        if ruby is None:
            self.skipTest("ruby is not available for YAML validation")
        skill_paths = sorted(PLUGIN_ROOT.glob("skills/*/SKILL.md"))
        self.assertGreater(len(skill_paths), 0)
        script = (
            "require 'yaml'; "
            "ARGV.each { |p| "
            "front = File.read(p, encoding: 'UTF-8').split(/^---\\s*$/, 3)[1]; "
            "raise \"missing frontmatter: #{p}\" unless front; "
            "data = YAML.safe_load(front); "
            "raise \"missing name: #{p}\" unless data['name']; "
            "raise \"missing description: #{p}\" unless data['description']; }"
        )
        subprocess.check_call([ruby, "-e", script, *map(str, skill_paths)])

    def test_agent_frontmatter_is_valid_yaml(self) -> None:
        # Shared agents are loaded by Claude Code, Cursor, and Gemini CLI;
        # `name` + `description` is the common frontmatter subset they all
        # accept. OpenCode agents add mode/permission in clients/opencode/.
        ruby = shutil.which("ruby")
        if ruby is None:
            self.skipTest("ruby is not available for YAML validation")
        shared = sorted(PLUGIN_ROOT.glob("agents/*.md"))
        opencode = sorted(PLUGIN_ROOT.glob("clients/opencode/agents/*.md"))
        self.assertGreater(len(shared), 0)
        self.assertEqual(
            [path.name for path in shared],
            [path.name for path in opencode],
            "OpenCode reviewer agents must mirror the shared agent set",
        )
        script = (
            "require 'yaml'; "
            "ARGV.each { |p| "
            "front = File.read(p, encoding: 'UTF-8').split(/^---\\s*$/, 3)[1]; "
            "raise \"missing frontmatter: #{p}\" unless front; "
            "data = YAML.safe_load(front); "
            "raise \"missing description: #{p}\" unless data['description']; "
            "if p.include?('clients/opencode/') then "
            "raise \"missing mode: #{p}\" unless data['mode'] == 'subagent'; "
            "raise \"missing permission: #{p}\" unless data['permission']; "
            "else "
            "raise \"missing name: #{p}\" unless data['name']; "
            "end }"
        )
        subprocess.check_call([ruby, "-e", script, *map(str, shared + opencode)])


if __name__ == "__main__":
    unittest.main()
