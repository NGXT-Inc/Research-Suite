"""Plane-boundary lints for the control/data split.

Phases 0–3 of docs/CLOUD_BACKEND_MIGRATION_PLAN.md: every tool contract
carries a plane, the three route sets partition the registry exactly, and the
modules that must stay cloud-servable do not grow local-process or local-path
dependencies. Hard from Phase 3: control modules cannot import subprocess,
the rsync/conn machinery, or the dataplane package, and the record store does
not know where the repository checkout lives.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from backend.contracts import (
    AGGREGATE_TOOL_NAMES,
    CONTROL_PLANE_TOOL_NAMES,
    DATA_PLANE_TOOL_NAMES,
    TOOL_CONTRACTS,
)
from tests.paths import BACKEND_ROOT, DOMAIN_ROOT, SERVICES_ROOT


# The only services modules allowed to spawn local processes (ssh/rsync/
# ssh-keygen/tunnels). Everything else in services/ must stay cloud-servable.
# sandbox_mgmt_keys is control-plane property (plan Phase 5) but mints keys
# with ssh-keygen — a process the control VM runs itself, never user-machine
# IO.
SUBPROCESS_ALLOWED = {
    "sandbox_conn.py",
    "sandbox_dashboards.py",
    "sandbox_mgmt_keys.py",
}

# Record halves that must be servable from a cloud control plane: no local
# processes, no rsync/conn machinery, no dataplane worker.
DOMAIN_MODULES = tuple(sorted(DOMAIN_ROOT.glob("*.py")))

CONTROL_MODULES = (
    *DOMAIN_MODULES,
    BACKEND_ROOT / "tool_facade.py",
    SERVICES_ROOT / "projects.py",
    SERVICES_ROOT / "claims.py",
    SERVICES_ROOT / "experiments.py",
    SERVICES_ROOT / "syntheses.py",
    SERVICES_ROOT / "reviews.py",
    SERVICES_ROOT / "workflow.py",
    SERVICES_ROOT / "workflow_views.py",
    SERVICES_ROOT / "experiment_views.py",
    SERVICES_ROOT / "permissions.py",
    SERVICES_ROOT / "artifacts.py",
    SERVICES_ROOT / "graph_lint.py",
    SERVICES_ROOT / "pinned.py",
    SERVICES_ROOT / "sync_sessions.py",
    SERVICES_ROOT / "metrics_records.py",
    BACKEND_ROOT / "state" / "store.py",
    BACKEND_ROOT / "state" / "dialects.py",
)

# Module names (any dotted segment) control modules may never import.
CONTROL_FORBIDDEN_SEGMENTS = {
    "dataplane",
    "sandbox_conn",
    "ssh_rsync",
    "subprocess",
    "workspace",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "__future__":
                continue
            modules.add(node.module.split(".", 1)[0])
    return modules


def _import_segments(path: Path) -> set[str]:
    """Every dotted segment of every imported module path.

    Catches relative submodule imports (``from ..execution.ssh_rsync import``)
    that a top-level-only collector would report as just ``execution``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    segments: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                segments.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            if node.module:
                segments.update(node.module.split("."))
            for alias in node.names:
                segments.update(alias.name.split("."))
    return segments


class ToolPlanePartitionTest(unittest.TestCase):
    def test_every_tool_has_a_plane(self) -> None:
        for name, contract in TOOL_CONTRACTS.items():
            self.assertIn(contract.plane, {"control", "data", "aggregate"}, name)

    def test_planes_partition_the_registry(self) -> None:
        union = CONTROL_PLANE_TOOL_NAMES | DATA_PLANE_TOOL_NAMES | AGGREGATE_TOOL_NAMES
        self.assertEqual(union, set(TOOL_CONTRACTS))
        self.assertFalse(CONTROL_PLANE_TOOL_NAMES & DATA_PLANE_TOOL_NAMES)
        self.assertFalse(CONTROL_PLANE_TOOL_NAMES & AGGREGATE_TOOL_NAMES)
        self.assertFalse(DATA_PLANE_TOOL_NAMES & AGGREGATE_TOOL_NAMES)

    def test_data_and_aggregate_assignments_are_pinned(self) -> None:
        # The routing table of CLOUD_BACKEND_MIGRATION_PLAN.md §3.3. Changing
        # these is changing where a tool is served in split mode — do it in the
        # phase diff that moves the behavior, not casually.
        self.assertEqual(
            DATA_PLANE_TOOL_NAMES,
            {
                "resource.register_file",
                "resource.associate",
                "sandbox.request",
                "sandbox.sync",
                # feed.post reads a local image file before recording the post,
                # so it lives on the data plane (byte capture mirrors
                # resource.associate); register/list are pure control records.
                "feed.post",
            },
        )
        self.assertEqual(AGGREGATE_TOOL_NAMES, {"sandbox.health", "sandbox.get"})


class PlaneImportLintTest(unittest.TestCase):
    def test_only_sandbox_io_modules_spawn_processes(self) -> None:
        for path in sorted(SERVICES_ROOT.glob("*.py")):
            if path.name in SUBPROCESS_ALLOWED:
                continue
            with self.subTest(module=path.name):
                self.assertNotIn("subprocess", _imports(path))

    def test_control_modules_import_no_local_io(self) -> None:
        # Hard from Phase 3: the record halves must be provably IO-free so the
        # same code can serve from a cloud VM with no checkout, no ssh, and no
        # worker in-process.
        for path in CONTROL_MODULES:
            with self.subTest(module=path.name):
                forbidden = _import_segments(path) & CONTROL_FORBIDDEN_SEGMENTS
                self.assertFalse(
                    forbidden,
                    f"{path.name} imports local-IO modules: {sorted(forbidden)}",
                )

    def test_state_store_knows_no_repo_root(self) -> None:
        # The record store is a records-only component (plan §3.1): local
        # paths belong to LocalWorkspace / the DataPlaneWorker.
        source = (BACKEND_ROOT / "state" / "store.py").read_text(encoding="utf-8")
        self.assertNotIn("repo_root", source)

    def test_telemetry_sinks_are_store_independent(self) -> None:
        # ActivityLogger and ToolCallStore are config-injected, machine-local
        # sinks (plan §3.2): they take explicit paths from the composition and
        # never reach into the record store.
        for name in ("activity.py", "tool_calls.py"):
            with self.subTest(module=name):
                source = (BACKEND_ROOT / "state" / name).read_text(encoding="utf-8")
                self.assertNotIn("store", _imports(BACKEND_ROOT / "state" / name))
                self.assertNotIn("StateStore", source)


class ProxyStdlibOnlyTest(unittest.TestCase):
    """The stdio MCP proxy must stay stdlib-only (cloud plan Phase 8 packaging).

    The proxy ships as part of the base wheel with no third-party deps so it
    runs under any Python — even the daemon profile that drops the provider
    SDKs. Walk the mcp_server package and assert no import resolves outside the
    stdlib (or the package itself), so the dual-upstream rewrite never reaches
    for pydantic/fastapi/boto3 by accident.
    """

    # The proxy's own package + the standard library are the only allowed
    # roots. (sys.stdlib_module_names covers the stdlib on 3.11+.)
    def test_mcp_server_imports_only_stdlib(self) -> None:
        import sys

        plugin_root = BACKEND_ROOT.parent
        mcp_root = plugin_root / "mcp_server"
        # The package's own siblings (imported relatively) plus the stdlib.
        own = {p.stem for p in mcp_root.glob("*.py")} | {"mcp_server"}
        allowed = set(sys.stdlib_module_names) | own | {"__future__"}
        for path in sorted(mcp_root.glob("*.py")):
            with self.subTest(module=path.name):
                external = _imports(path) - allowed
                self.assertFalse(
                    external,
                    f"{path.name} imports non-stdlib modules: {sorted(external)}",
                )


if __name__ == "__main__":
    unittest.main()
