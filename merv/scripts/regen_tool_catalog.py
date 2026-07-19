#!/usr/bin/env python3
"""Regenerate src/merv/proxy/_tool_catalog.json from the live tool contracts.

Run after changing src/merv/brain/tools/contracts.py; the surface test
tests/surface/test_static_tool_catalog.py fails until the checked-in file
matches the live render.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from merv.proxy.proxy import _STATIC_CATALOG_PATH, _render_static_catalog_text


def main() -> int:
    _STATIC_CATALOG_PATH.write_text(_render_static_catalog_text(), encoding="utf-8")
    print(f"wrote {_STATIC_CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
