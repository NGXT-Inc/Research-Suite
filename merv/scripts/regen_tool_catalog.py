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

from merv.brain.surface.tools.contracts import (
    DATA_PLANE_TOOL_NAMES,
    proxy_tool_manifest,
    static_tool_catalog,
)


_STATIC_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "merv"
    / "proxy"
    / "_tool_catalog.json"
)
_PROXY_MANIFEST_PATH = _STATIC_CATALOG_PATH.with_name("_tool_manifest.json")


def render_static_catalog_text() -> str:
    """Render the full proxy catalog; runtime applies the storage filter."""
    allowed = DATA_PLANE_TOOL_NAMES | {
        tool["name"]
        for tool in proxy_tool_manifest()
        if tool["executionStrategy"] == "control-plus-local-enrichment"
    }
    tools = [
        tool
        for tool in static_tool_catalog(storage_enabled=True)
        if tool.get("name") in allowed
    ]
    return json.dumps({"tools": tools}, indent=2, sort_keys=True) + "\n"


def render_proxy_manifest_text() -> str:
    """Render the private all-tool routing manifest shipped with the client."""
    return json.dumps({"tools": proxy_tool_manifest()}, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in catalog differs without writing it",
    )
    args = parser.parse_args(argv)
    rendered = {
        _STATIC_CATALOG_PATH: render_static_catalog_text(),
        _PROXY_MANIFEST_PATH: render_proxy_manifest_text(),
    }
    if args.check:
        stale = [path for path, text in rendered.items() if not path.exists() or path.read_text(encoding="utf-8") != text]
        if stale:
            print(f"stale {', '.join(map(str, stale))}; run scripts/regen_tool_catalog.py", file=sys.stderr)
            return 1
        for path in rendered:
            print(f"ok {path}")
        return 0
    for path, text in rendered.items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
