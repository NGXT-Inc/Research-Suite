#!/usr/bin/env python3
"""Regenerate src/merv/proxy/_tool_catalog.json from the live tool contracts.

Run after changing src/merv/brain/surface/tools/contracts.py; the surface test
tests/surface/test_static_tool_catalog.py fails until the checked-in file
matches the live render.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from merv.brain.surface.tools.contracts import DATA_PLANE_TOOL_NAMES, static_tool_catalog


_STATIC_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "merv"
    / "proxy"
    / "_tool_catalog.json"
)
_LOCAL_ENRICHED_CONTROL_TOOLS = frozenset({"sandbox.get", "sandbox.health"})


def render_static_catalog_text() -> str:
    """Render the full proxy catalog; runtime applies the storage filter."""
    allowed = DATA_PLANE_TOOL_NAMES | _LOCAL_ENRICHED_CONTROL_TOOLS
    tools = [
        tool
        for tool in static_tool_catalog(storage_enabled=True)
        if tool.get("name") in allowed
    ]
    return json.dumps({"tools": tools}, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in catalog differs without writing it",
    )
    args = parser.parse_args(argv)
    rendered = render_static_catalog_text()
    if args.check:
        current = _STATIC_CATALOG_PATH.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"stale {_STATIC_CATALOG_PATH}; run scripts/regen_tool_catalog.py",
                file=sys.stderr,
            )
            return 1
        print(f"ok {_STATIC_CATALOG_PATH}")
        return 0
    _STATIC_CATALOG_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {_STATIC_CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
