from __future__ import annotations

from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_ROOT.parent
# The sys.path/PYTHONPATH entry that makes the shipped packages importable.
IMPORT_ROOT = PLUGIN_ROOT
BACKEND_ROOT = PLUGIN_ROOT / "backend"
PROXY_ROOT = PLUGIN_ROOT / "mcp_server"
SHARED_ROOT = PLUGIN_ROOT / "research_plugin_shared"
ARTIFACTS_ROOT = BACKEND_ROOT / "artifacts"
DOMAIN_ROOT = BACKEND_ROOT / "domain"
FEED_ROOT = BACKEND_ROOT / "feed"
PORTS_ROOT = BACKEND_ROOT / "kernel" / "ports"
RESEARCH_CORE_ROOT = BACKEND_ROOT / "research_core"
SERVICES_ROOT = BACKEND_ROOT / "services"
