#!/usr/bin/env python3
"""Regenerate agents/<name>.md bodies from their canonical reviewer skills.

The three reviewer instruction sets are authored once in
skills/<name>/SKILL.md; the shared agents/<name>.md files (loaded by Claude
Code, Cursor, and Gemini CLI) keep their own frontmatter but carry the same
body verbatim. Run this after editing a reviewer skill; the surface test
tests/surface/test_reviewer_instruction_parity.py fails until the checked-in
agent files match the render. (OpenCode's clients/opencode/agents/ stubs are
thin delegates that load the skill at runtime and are not generated.)
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

REVIEWER_NAMES = (
    "experiment-design-review",
    "experiment-attempt-review",
    "project-reflection-review",
)

GENERATED_MARKER = (
    "<!-- Body generated from skills/{name}/SKILL.md by"
    " scripts/regen_reviewer_agents.py — edit the skill, then regenerate. -->"
)


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    """Return (frontmatter block including both --- fences, body)."""
    parts = text.split("---\n", 2)
    if len(parts) != 3 or parts[0] != "":
        raise ValueError(f"missing frontmatter: {path}")
    frontmatter = f"---\n{parts[1]}---\n"
    return frontmatter, parts[2].lstrip("\n")


def render_agent_text(name: str) -> str:
    skill_path = PLUGIN_ROOT / "skills" / name / "SKILL.md"
    agent_path = PLUGIN_ROOT / "agents" / f"{name}.md"
    _, body = _split_frontmatter(skill_path.read_text(encoding="utf-8"), skill_path)
    frontmatter, _ = _split_frontmatter(agent_path.read_text(encoding="utf-8"), agent_path)
    marker = GENERATED_MARKER.format(name=name)
    return f"{frontmatter}\n{marker}\n\n{body}"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale: list[str] = []
    for name in REVIEWER_NAMES:
        agent_path = PLUGIN_ROOT / "agents" / f"{name}.md"
        rendered = render_agent_text(name)
        if agent_path.read_text(encoding="utf-8") != rendered:
            stale.append(str(agent_path))
            if not check:
                agent_path.write_text(rendered, encoding="utf-8")
                print(f"wrote {agent_path}")
    if check and stale:
        print("stale (run scripts/regen_reviewer_agents.py):")
        for path in stale:
            print(f"  {path}")
        return 1
    if not stale:
        print("agents already match their skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
