from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class PluginSkillTest(unittest.TestCase):
    def test_skill_frontmatter_is_valid_yaml(self) -> None:
        ruby = shutil.which("ruby")
        if ruby is None:
            self.skipTest("ruby is not available for YAML validation")
        skill_paths = sorted(Path(__file__).resolve().parents[1].glob("skills/*/SKILL.md"))
        self.assertGreater(len(skill_paths), 0)
        script = (
            "require 'yaml'; "
            "ARGV.each { |p| front = File.read(p).split(/^---\\s*$/, 3)[1]; "
            "raise \"missing frontmatter: #{p}\" unless front; "
            "data = YAML.safe_load(front); "
            "raise \"missing name: #{p}\" unless data['name']; "
            "raise \"missing description: #{p}\" unless data['description']; }"
        )
        subprocess.check_call([ruby, "-e", script, *map(str, skill_paths)])


if __name__ == "__main__":
    unittest.main()
