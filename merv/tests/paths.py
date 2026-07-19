from __future__ import annotations

from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_ROOT.parent
# The sys.path/PYTHONPATH entry that makes the shipped packages importable.
IMPORT_ROOT = PLUGIN_ROOT / "src"
BACKEND_ROOT = IMPORT_ROOT / "merv" / "brain"
PROXY_ROOT = IMPORT_ROOT / "merv" / "proxy"
SHARED_ROOT = IMPORT_ROOT / "merv" / "shared"
ARTIFACTS_ROOT = BACKEND_ROOT / "artifacts"
FEED_ROOT = BACKEND_ROOT / "feed"
PORTS_ROOT = BACKEND_ROOT / "kernel" / "ports"
RESEARCH_CORE_ROOT = BACKEND_ROOT / "research_core"
DOMAIN_ROOT = RESEARCH_CORE_ROOT / "domain"
# Post-fold alias: the glue services live flat in surface/. Never rglob this
# root for "service" sweeps — surface/ also holds tools/transport/composition;
# pin file lists explicitly (see GLUE_SERVICE_FILES in the structure tests).
SERVICES_ROOT = BACKEND_ROOT / "surface"
SURFACE_ROOT = BACKEND_ROOT / "surface"
CLIENT_ROOT = IMPORT_ROOT / "merv" / "client"
