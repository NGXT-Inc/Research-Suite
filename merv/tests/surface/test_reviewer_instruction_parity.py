"""The reviewer skills are the single source of the reviewer instructions.

skills/<name>/SKILL.md is canonical; the shared agents/<name>.md files keep
their own frontmatter but must carry the skill body verbatim (regenerate with
scripts/regen_reviewer_agents.py). OpenCode's per-client stubs are not
generated — they delegate by loading the skill at runtime — so they must at
least name the skill they load.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from tests.paths import PLUGIN_ROOT


def _load_regen_module():
    path = PLUGIN_ROOT / "scripts" / "regen_reviewer_agents.py"
    spec = importlib.util.spec_from_file_location("regen_reviewer_agents", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewerInstructionParityTest(unittest.TestCase):
    def test_agent_files_match_their_skill_render(self) -> None:
        regen = _load_regen_module()
        for name in regen.REVIEWER_NAMES:
            agent_path = PLUGIN_ROOT / "agents" / f"{name}.md"
            with self.subTest(agent=name):
                self.assertEqual(
                    agent_path.read_text(encoding="utf-8"),
                    regen.render_agent_text(name),
                    f"{agent_path} is stale — run scripts/regen_reviewer_agents.py",
                )

    def test_opencode_stubs_delegate_to_their_skill(self) -> None:
        regen = _load_regen_module()
        for name in regen.REVIEWER_NAMES:
            stub_path = PLUGIN_ROOT / "clients" / "opencode" / "agents" / f"{name}.md"
            with self.subTest(agent=name):
                self.assertIn(
                    f"`{name}` skill",
                    stub_path.read_text(encoding="utf-8"),
                    f"{stub_path} must instruct loading the {name} skill",
                )

    def test_reviewer_names_cover_all_shared_agents(self) -> None:
        regen = _load_regen_module()
        shared = sorted(path.stem for path in (PLUGIN_ROOT / "agents").glob("*.md"))
        self.assertEqual(shared, sorted(regen.REVIEWER_NAMES))


if __name__ == "__main__":
    unittest.main()
