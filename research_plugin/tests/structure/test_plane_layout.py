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
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import get_type_hints

from backend.contracts import (
    AGGREGATE_TOOL_NAMES,
    CONTROL_PLANE_TOOL_NAMES,
    DATA_PLANE_TOOL_NAMES,
    TOOL_CONTRACTS,
)
from tests.paths import BACKEND_ROOT, DOMAIN_ROOT, PORTS_ROOT, SERVICES_ROOT


# Record halves that must be servable from a cloud control plane: no local
# processes, no rsync/conn machinery, no dataplane worker.
DOMAIN_MODULES = tuple(sorted(DOMAIN_ROOT.glob("*.py")))
PORT_MODULES = tuple(sorted(PORTS_ROOT.glob("*.py")))

CONTROL_MODULES = (
    *DOMAIN_MODULES,
    *PORT_MODULES,
    BACKEND_ROOT / "sandbox_backend.py",
    BACKEND_ROOT / "tool_facade.py",
    BACKEND_ROOT / "tool_handlers.py",
    SERVICES_ROOT / "projects.py",
    SERVICES_ROOT / "claims.py",
    SERVICES_ROOT / "experiments.py",
    SERVICES_ROOT / "syntheses.py",
    SERVICES_ROOT / "reviews.py",
    SERVICES_ROOT / "workflow.py",
    SERVICES_ROOT / "workflow_views.py",
    SERVICES_ROOT / "experiment_views.py",
    SERVICES_ROOT / "permissions.py",
    SERVICES_ROOT / "project_overview.py",
    SERVICES_ROOT / "reflection_tools.py",
    SERVICES_ROOT / "pinned.py",
    SERVICES_ROOT / "resources.py",
    SERVICES_ROOT / "feed.py",
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


def _top_level_import_segments(path: Path) -> set[str]:
    """Every dotted segment imported from module-top-level import statements."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    segments: set[str] = set()
    for node in tree.body:
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


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _call_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return ""


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _resolve_call_name(name: str, aliases: dict[str, str]) -> str:
    if not name:
        return ""
    parts = name.split(".", 1)
    head = aliases.get(parts[0], parts[0])
    return f"{head}.{parts[1]}" if len(parts) == 2 else head


def _literal_args(node: ast.Call) -> list[str]:
    values: list[str] = []
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            values.append(arg.value)
    return values


def _process_spawn_references(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _import_aliases(tree)
    references: set[str] = set()
    spawn_calls = {
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.popen",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "os.system",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.run",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    references.add("import subprocess")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                references.add("from subprocess import ...")
        elif isinstance(node, ast.Call):
            name = _resolve_call_name(_call_name(node.func), aliases)
            if name in spawn_calls:
                references.add(name)
            if name == "__import__" and "subprocess" in _literal_args(node):
                references.add("__import__('subprocess')")
            if name == "importlib.import_module" and "subprocess" in _literal_args(node):
                references.add("importlib.import_module('subprocess')")
    return references


def _imports_management_key_adapter(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("state.mgmt_keys"):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if module.endswith("state.mgmt_keys"):
                return True
            if module.endswith("state") and any(
                alias.name == "mgmt_keys" for alias in node.names
            ):
                return True
    return False


def _method_name_literal_branches(
    path: Path, *, class_name: str, method_name: str
) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method: ast.FunctionDef | None = None
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == method_name:
                method = item
                break
    if method is None:
        raise AssertionError(f"{class_name}.{method_name} not found in {path}")
    names: set[str] = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "name":
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            if isinstance(op, ast.Eq):
                if isinstance(comparator, ast.Constant) and isinstance(
                    comparator.value, str
                ):
                    names.add(comparator.value)
    return names


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

    def test_daemon_tool_catalogs_use_contract_plane_sets(self) -> None:
        daemon_mode = BACKEND_ROOT / "composition" / "daemon_mode.py"
        for path in (daemon_mode, BACKEND_ROOT / "daemon_loopback.py"):
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertIn("DATA_PLANE_TOOL_NAMES", source)
                self.assertIn(
                    "allowed = DATA_PLANE_TOOL_NAMES | AGGREGATE_TOOL_NAMES",
                    source,
                )
                self.assertNotIn("IMPLEMENTED_DATA_TOOL_NAMES", source)
                self.assertNotIn("IMPLEMENTED_LOOPBACK_DATA_TOOL_NAMES", source)
        self.assertTrue(
            DATA_PLANE_TOOL_NAMES
            <= _method_name_literal_branches(
                daemon_mode, class_name="DaemonServer", method_name="call_tool"
            )
        )


class PlaneImportLintTest(unittest.TestCase):
    def test_process_spawn_lint_catches_alias_forms(self) -> None:
        source = """
import os as ops
from os import system as run_cmd
from asyncio import create_subprocess_exec
from importlib import import_module as load

ops.popen("cmd")
run_cmd("cmd")
create_subprocess_exec("cmd")
load("subprocess")
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.py"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(
                _process_spawn_references(path),
                {
                    "os.popen",
                    "os.system",
                    "asyncio.create_subprocess_exec",
                    "importlib.import_module('subprocess')",
                },
            )

    def test_management_key_adapter_lint_catches_import_forms(self) -> None:
        cases = (
            "import backend.state.mgmt_keys\n",
            "from backend.state import mgmt_keys\n",
            "from ..state.mgmt_keys import LocalMgmtKeyStore\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            for index, source in enumerate(cases):
                path = Path(tmp) / f"service_{index}.py"
                path.write_text(source, encoding="utf-8")
                with self.subTest(source=source.strip()):
                    self.assertTrue(_imports_management_key_adapter(path))

    def test_only_sandbox_io_modules_spawn_processes(self) -> None:
        for path in sorted(SERVICES_ROOT.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertFalse(
                    _process_spawn_references(path),
                    f"{path.name} references process-spawn APIs",
                )

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

    def test_sync_record_views_do_not_import_execution(self) -> None:
        # Remote directory names and session version pins are a control/data
        # contract, not provider execution machinery.
        for name in ("sync_sessions.py", "sandbox_views.py"):
            with self.subTest(module=name):
                self.assertNotIn("execution", _import_segments(SERVICES_ROOT / name))
        source = (SERVICES_ROOT / "sync_sessions.py").read_text(encoding="utf-8")
        imports = _import_segments(SERVICES_ROOT / "sync_sessions.py")
        self.assertIn("sandbox_sync", imports)
        self.assertIn("registry: RunningSandboxRows", source)
        self.assertIn("list_running_sync_rows", source)
        self.assertNotIn("list_running_rows", source)
        self.assertNotIn("class RunningSandboxRows", source)

    def test_sandbox_services_use_backend_port_not_execution_package(self) -> None:
        # Record/control sandbox services depend on the provider-neutral port,
        # while concrete provider machinery stays under execution/.
        for name in ("sandbox_daemons.py", "sandbox_provisioner.py", "sandboxes.py"):
            with self.subTest(module=name):
                self.assertNotIn("execution", _import_segments(SERVICES_ROOT / name))

    def test_sandbox_backend_port_is_neutral(self) -> None:
        imports = _import_segments(BACKEND_ROOT / "sandbox_backend.py")
        forbidden = imports & {
            "dataplane",
            "execution",
            "services",
            "ssh_rsync",
            "state",
            "subprocess",
            "workspace",
        }
        self.assertFalse(
            forbidden,
            f"sandbox backend port imports backend layers: {sorted(forbidden)}",
        )

    def test_telemetry_sinks_are_store_independent(self) -> None:
        # ActivityLogger and ToolCallStore are config-injected, machine-local
        # sinks (plan §3.2): they take explicit paths from the composition and
        # never reach into the record store.
        for name in ("activity.py", "tool_calls.py"):
            with self.subTest(module=name):
                source = (BACKEND_ROOT / "state" / name).read_text(encoding="utf-8")
                self.assertNotIn("store", _imports(BACKEND_ROOT / "state" / name))
                self.assertNotIn("StateStore", source)

    def test_services_package_init_is_import_light(self) -> None:
        # Importing a control-safe service submodule executes services/__init__.
        # Keep the package initializer inert so a future ControlApp can import
        # individual record/view services without loading data-plane services.
        self.assertFalse(_imports(SERVICES_ROOT / "__init__.py"))

    def test_sandbox_support_is_neutral(self) -> None:
        # Shared sandbox constants/helpers are used by both services and the
        # data plane. Keep this module below both packages so it cannot become
        # a backdoor import path between them.
        imports = _import_segments(BACKEND_ROOT / "sandbox_support.py")
        for forbidden in (
            "services",
            "dataplane",
            "workspace",
            "ssh_rsync",
            "subprocess",
            "threading",
        ):
            self.assertNotIn(forbidden, imports)
        code = """
import sys
import backend.sandbox_support
for name in (
    "backend.services.sandboxes",
    "backend.dataplane.worker",
    "backend.execution.ssh_rsync",
    "backend.workspace",
):
    if name in sys.modules:
        raise SystemExit(f"{name} loaded")
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(BACKEND_ROOT.parent)
        subprocess.run([sys.executable, "-c", code], check=True, env=env)

    def test_dataplane_package_init_is_import_light(self) -> None:
        # Importing dataplane.tasks should not pull in the local worker package
        # barrel. The worker stays available through lazy __getattr__ exports.
        imports = _top_level_import_segments(BACKEND_ROOT / "dataplane" / "__init__.py")
        for forbidden in ("worker", "ssh_rsync", "workspace"):
            self.assertNotIn(forbidden, imports)
        code = """
import sys
import backend.dataplane.tasks
for name in (
    "backend.dataplane.worker",
    "backend.execution.ssh_rsync",
    "backend.workspace",
):
    if name in sys.modules:
        raise SystemExit(f"{name} loaded")
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(BACKEND_ROOT.parent)
        subprocess.run([sys.executable, "-c", code], check=True, env=env)

    def test_dataplane_modules_do_not_import_services(self) -> None:
        # The data plane executes contracts minted by control services; it
        # should not import those services to learn the contract shape.
        for path in sorted((BACKEND_ROOT / "dataplane").glob("*.py")):
            with self.subTest(module=path.name):
                self.assertNotIn("services", _import_segments(path))

    def test_remote_control_view_uses_sync_target_port(self) -> None:
        imports = _import_segments(BACKEND_ROOT / "dataplane" / "remote_view.py")
        self.assertIn("sandbox_sync", imports)
        from backend.dataplane.remote_view import HttpControlPlaneView
        from backend.ports.sandbox_sync import SyncTarget

        self.assertEqual(
            get_type_hints(HttpControlPlaneView.sync_targets)["return"],
            list[SyncTarget],
        )

    def test_project_overview_does_not_import_mutation_services(self) -> None:
        # project.current is a read-side control projection. Keep it decoupled
        # from mutation services so a future ControlApp can compose the view
        # with narrower readers.
        imports = _import_segments(SERVICES_ROOT / "project_overview.py")
        self.assertNotIn("projects", imports)
        self.assertNotIn("syntheses", imports)
        self.assertIn("project_readers", imports)
        source = (SERVICES_ROOT / "project_overview.py").read_text(encoding="utf-8")
        self.assertIn("projects: ProjectCurrentReader", source)
        self.assertIn("syntheses: SynthesisOverviewReader", source)
        self.assertNotIn("class ProjectCurrentReader", source)
        self.assertNotIn("class SynthesisOverviewReader", source)

    def test_workflow_service_uses_reader_ports(self) -> None:
        # workflow.status_and_next is a control-safe orientation view. It
        # should compose against narrow read ports instead of redeclaring
        # collaborator protocols inside the workflow service module.
        imports = _import_segments(SERVICES_ROOT / "workflow.py")
        self.assertFalse(
            {"experiments", "reviews", "sandboxes", "syntheses"} & imports
        )
        self.assertIn("workflow_readers", imports)
        source = (SERVICES_ROOT / "workflow.py").read_text(encoding="utf-8")
        self.assertIn("experiments: ExperimentWorkflowReader", source)
        self.assertNotIn("class ExperimentWorkflowReader", source)
        self.assertNotIn("class ReviewWorkflowReader", source)
        self.assertNotIn("class SandboxWorkflowReader", source)
        self.assertNotIn("class ReflectionWorkflowReader", source)

    def test_resource_service_uses_record_ports(self) -> None:
        # ResourceService is the control-safe record half. Local observation
        # belongs to the local composition edge, not to the record service.
        imports = _import_segments(SERVICES_ROOT / "resources.py")
        self.assertIn("resource_records", imports)
        self.assertNotIn("permissions", imports)
        source = (SERVICES_ROOT / "resources.py").read_text(encoding="utf-8")
        self.assertIn("permissions: ResourceAssociationPolicy", source)
        self.assertNotIn("observer: ResourceObserver", source)
        self.assertNotIn("self.observer", source)
        self.assertNotIn("def register_file(", source)
        self.assertNotIn("class ResourceObserver", source)
        self.assertNotIn("class ResourceAssociationPolicy", source)
        app_source = (BACKEND_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("def register_resource_file(", app_source)
        self.assertIn("resource_register_file=self.register_resource_file", app_source)
        self.assertIn("self.resource_observer.observe_file", app_source)

    def test_reflection_tools_do_not_import_mutation_service(self) -> None:
        # reflection.* is a tool-namespace adapter. It should compose against a
        # narrow protocol instead of importing the internal synthesis mutation
        # service just to translate public names.
        imports = _import_segments(SERVICES_ROOT / "reflection_tools.py")
        self.assertNotIn("syntheses", imports)
        self.assertIn("reflection_waves", imports)
        source = (SERVICES_ROOT / "reflection_tools.py").read_text(encoding="utf-8")
        self.assertIn("syntheses: ReflectionWaveStore", source)
        self.assertNotIn("class ReflectionWaveStore", source)

    def test_app_keeps_concrete_local_runtime_wiring_in_one_module(self) -> None:
        # This is an incremental local-mode extraction, not a ControlApp split:
        # ResearchPluginApp may still depend on local_runtime, but concrete
        # filesystem/worker/default-backend classes stay out of app.py.
        source = (BACKEND_ROOT / "app.py").read_text(encoding="utf-8")
        for forbidden in (
            "LocalWorkspace",
            "LocalDataPlaneWorker",
            "LocalDirBlobStore",
            "ActivityLogger",
            "ToolCallStore",
            "LocalMgmtKeyStore",
            "build_sandbox_backend",
        ):
            self.assertNotIn(forbidden, source)

    def test_management_key_store_is_adapter_not_service(self) -> None:
        # The service layer depends on the MgmtKeyStore port only. The local
        # filesystem/ssh-keygen implementation belongs to composition-state
        # wiring, not services/.
        self.assertFalse((SERVICES_ROOT / "sandbox_mgmt_keys.py").exists())
        for path in sorted(SERVICES_ROOT.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertFalse(_imports_management_key_adapter(path))
                self.assertNotIn("LocalMgmtKeyStore", path.read_text(encoding="utf-8"))
        imports = _import_segments(BACKEND_ROOT / "state" / "mgmt_keys.py")
        self.assertIn("subprocess", imports)
        self.assertNotIn("services", imports)
        self.assertIn("mgmt_keys", _import_segments(BACKEND_ROOT / "local_runtime.py"))

    def test_app_keeps_local_runtime_module_import_lazy(self) -> None:
        # Importing backend.app should not import backend.local_runtime itself.
        # Other local/data collaborators still need their own extraction chunks.
        imports = _top_level_import_segments(BACKEND_ROOT / "app.py")
        self.assertNotIn("local_runtime", imports)

    def test_app_import_keeps_local_io_modules_unloaded(self) -> None:
        # Import-time separation is not a full ControlApp, but app import should
        # not pull in local workspace, rsync, or data-plane worker machinery.
        code = """
import sys
import backend.app
for name in (
    "backend.local_runtime",
    "backend.workspace",
    "backend.dataplane.worker",
    "backend.dataplane.metrics_archive",
    "backend.dataplane.sandbox_dashboards",
    "backend.dataplane.sandbox_conn",
    "backend.execution.ssh_rsync",
    "backend.services.sandbox_conn",
    "backend.state.mgmt_keys",
):
    if name in sys.modules:
        raise SystemExit(f"{name} loaded")
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(BACKEND_ROOT.parent)
        subprocess.run([sys.executable, "-c", code], check=True, env=env)


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
