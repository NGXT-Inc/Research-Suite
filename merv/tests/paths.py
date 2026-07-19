from __future__ import annotations

from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_ROOT.parent
BACKEND_ROOT = PLUGIN_ROOT / "backend"
ARTIFACTS_ROOT = BACKEND_ROOT / "artifacts"
DOMAIN_ROOT = BACKEND_ROOT / "domain"
PORTS_ROOT = BACKEND_ROOT / "kernel" / "ports"
SERVICES_ROOT = BACKEND_ROOT / "services"
