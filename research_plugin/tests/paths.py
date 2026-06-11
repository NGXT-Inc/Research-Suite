from __future__ import annotations

from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_ROOT.parent
BACKEND_ROOT = PLUGIN_ROOT / "backend"
SERVICES_ROOT = BACKEND_ROOT / "services"
