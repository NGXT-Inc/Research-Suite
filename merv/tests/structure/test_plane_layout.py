"""Plane-boundary lints for the control/data split.

The split architecture in docs/CONTROL_DATA_PLANE_SPLIT.md requires every tool
has one catalog plane, the two route sets partition the registry exactly,
and the modules that must stay cloud-servable do not grow local-process or
local-path dependencies. Control modules cannot import subprocess, conn
machinery, or the dataplane package, and the record store does not know where
the repository checkout lives.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, get_type_hints

from backend.tools.contracts import (
    CONTROL_PLANE_TOOL_NAMES,
    DATA_PLANE_TOOL_NAMES,
    TOOL_CONTRACTS,
    TOOL_PLANE_REGISTRY,
)
from tests.paths import (
    ARTIFACTS_ROOT,
    BACKEND_ROOT,
    DOMAIN_ROOT,
    PLUGIN_ROOT,
    PORTS_ROOT,
    SERVICES_ROOT,
)


# Record halves that must be servable from a cloud control plane: no local
# processes, no conn machinery, no dataplane worker.
ARTIFACTS_MODULES = tuple(sorted(ARTIFACTS_ROOT.glob("*.py")))
DOMAIN_MODULES = tuple(sorted(DOMAIN_ROOT.glob("*.py")))
PORT_MODULES = tuple(sorted(PORTS_ROOT.glob("*.py")))

CONTROL_MODULES = (
    *ARTIFACTS_MODULES,
    *DOMAIN_MODULES,
    *PORT_MODULES,
    BACKEND_ROOT / "sandbox" / "sandbox_backend.py",
    BACKEND_ROOT / "tools" / "tool_facade.py",
    BACKEND_ROOT / "tools" / "tool_handlers.py",
    SERVICES_ROOT / "projects.py",
    SERVICES_ROOT / "claims.py",
    SERVICES_ROOT / "experiments.py",
    SERVICES_ROOT / "reflections.py",
    SERVICES_ROOT / "reviews.py",
    SERVICES_ROOT / "workflow.py",
    SERVICES_ROOT / "workflow_views.py",
    SERVICES_ROOT / "experiment_views.py",
    SERVICES_ROOT / "permissions.py",
    SERVICES_ROOT / "project_overview.py",
    SERVICES_ROOT / "reflection_tools.py",
    SERVICES_ROOT / "feed.py",
    BACKEND_ROOT / "sandbox" / "sandbox_metrics.py",
    BACKEND_ROOT / "control" / "record_core.py",
    BACKEND_ROOT / "control" / "control_app.py",
    BACKEND_ROOT / "control" / "control_runtime.py",
    BACKEND_ROOT / "control" / "control_client.py",
    BACKEND_ROOT / "kernel" / "state" / "store.py",
    BACKEND_ROOT / "kernel" / "state" / "dialects.py",
    BACKEND_ROOT / "sandbox" / "managed_mgmt_keys.py",
)

# Module names (any dotted segment) control modules may never import.
CONTROL_FORBIDDEN_SEGMENTS = {
    "dataplane",
    "sandbox_conn",
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

    Catches relative submodule imports that a top-level-only collector would
    report by parent package only.
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


def _class_method_names(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef)
            }
    raise AssertionError(f"{class_name} not found in {path}")


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
                if alias.name.endswith("sandbox.mgmt_keys"):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if module.endswith("sandbox.mgmt_keys"):
                return True
            if module.endswith("sandbox") and any(
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
        self.assertEqual(set(TOOL_PLANE_REGISTRY), set(TOOL_CONTRACTS))
        for name, plane in TOOL_PLANE_REGISTRY.items():
            self.assertIn(plane, {"control", "data"}, name)

    def test_planes_partition_the_registry(self) -> None:
        union = CONTROL_PLANE_TOOL_NAMES | DATA_PLANE_TOOL_NAMES
        self.assertEqual(union, set(TOOL_CONTRACTS))
        self.assertFalse(CONTROL_PLANE_TOOL_NAMES & DATA_PLANE_TOOL_NAMES)

    def test_data_assignments_are_pinned(self) -> None:
        # The routing table from docs/CONTROL_DATA_PLANE_SPLIT.md. Changing
        # these is changing where a tool is served in split mode — do it in the
        # phase diff that moves the behavior, not casually.
        self.assertEqual(
            DATA_PLANE_TOOL_NAMES,
            {
                "experiment.materialize_folders",
                "resource.register",
                "storage.upload_file",
                "storage.download_file",
                "sandbox.request",
                "sandbox.attach",
                "sandbox.pull_outputs",
                # feed.post reads a local image file before recording the post,
                # so it lives on the data plane (byte capture mirrors
                # resource.register); find/delete are pure control records.
                "feed.post",
            },
        )
        self.assertIn("sandbox.health", CONTROL_PLANE_TOOL_NAMES)
        self.assertIn("sandbox.get", CONTROL_PLANE_TOOL_NAMES)

    def test_local_repo_file_readers_are_data_plane(self) -> None:
        self.assertLessEqual(
            {
                "resource.register",
                "storage.upload_file",
                "feed.post",
            },
            DATA_PLANE_TOOL_NAMES,
        )

    def test_http_data_plane_features_point_at_data_plane_tools(self) -> None:
        from backend.transport.http_policy import HTTP_DATA_PLANE_FEATURE_TO_TOOL

        self.assertEqual(
            HTTP_DATA_PLANE_FEATURE_TO_TOOL,
            {
                "resource_registration": "resource.register",
                "resource_association": "resource.register",
            },
        )
        self.assertLessEqual(set(HTTP_DATA_PLANE_FEATURE_TO_TOOL.values()), DATA_PLANE_TOOL_NAMES)

    def test_proxy_local_data_plane_dispatches_contract_plane_sets(self) -> None:
        proxy = PLUGIN_ROOT / "mcp_server" / "proxy.py"
        local_data_plane = PLUGIN_ROOT / "mcp_server" / "local_data_plane.py"
        source = proxy.read_text(encoding="utf-8")

        self.assertIn("DATA_PLANE_TOOL_NAMES", source)
        self.assertIn("_LOCAL_ENRICHED_CONTROL_TOOLS", source)
        self.assertTrue(
            DATA_PLANE_TOOL_NAMES
            <= _method_name_literal_branches(
                local_data_plane,
                class_name="LocalDataPlane",
                method_name="call_tool",
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
            "import backend.sandbox.mgmt_keys\n",
            "from backend.sandbox import mgmt_keys\n",
            "from ..sandbox.mgmt_keys import LocalMgmtKeyStore\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            for index, source in enumerate(cases):
                path = Path(tmp) / f"service_{index}.py"
                path.write_text(source, encoding="utf-8")
                with self.subTest(source=source.strip()):
                    self.assertTrue(_imports_management_key_adapter(path))

    def test_only_sandbox_io_modules_spawn_processes(self) -> None:
        for path in sorted(SERVICES_ROOT.rglob("*.py")):
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

    def test_tool_dispatcher_uses_narrow_permission_policy(self) -> None:
        from backend.tools.tool_facade import ToolDispatcher, ToolPermissionPolicy

        hints = get_type_hints(ToolDispatcher.__init__)
        self.assertIs(hints["permissions"], ToolPermissionPolicy)
        self.assertIn(Protocol, ToolPermissionPolicy.__mro__)
        path = BACKEND_ROOT / "tools" / "tool_facade.py"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("permissions: Any", source)
        self.assertEqual(
            _class_method_names(path, "ToolPermissionPolicy"),
            {"reject_reviewer_mutation"},
        )
        tree = ast.parse(source)
        calls = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "permissions"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        }
        self.assertEqual(calls, {"reject_reviewer_mutation"})

    def test_state_store_knows_no_repo_root(self) -> None:
        # The record store is a records-only component (plan §3.1): local
        # paths belong to LocalWorkspace / the DataPlaneWorker.
        source = (BACKEND_ROOT / "kernel" / "state" / "store.py").read_text(encoding="utf-8")
        self.assertNotIn("repo_root", source)

    def test_sandbox_views_do_not_import_execution(self) -> None:
        # Remote directory names are projected without provider execution machinery.
        path = BACKEND_ROOT / "sandbox" / "sandbox_views.py"
        self.assertNotIn("execution", _import_segments(path))

    def test_sandbox_services_use_backend_port_not_execution_package(self) -> None:
        # Record/control sandbox services depend on the provider-neutral port,
        # while concrete provider machinery stays under execution/.
        for name in ("sandbox_daemons.py", "sandbox_provisioner.py", "sandboxes.py"):
            with self.subTest(module=name):
                self.assertNotIn(
                    "execution", _import_segments(BACKEND_ROOT / "sandbox" / name)
                )

    def test_sandbox_backend_port_is_neutral(self) -> None:
        imports = _import_segments(BACKEND_ROOT / "sandbox" / "sandbox_backend.py")
        forbidden = imports & {
            "dataplane",
            "execution",
            "services",
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
                source = (BACKEND_ROOT / "kernel" / "state" / name).read_text(encoding="utf-8")
                self.assertNotIn("store", _imports(BACKEND_ROOT / "kernel" / "state" / name))
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
        imports = _import_segments(BACKEND_ROOT / "sandbox" / "sandbox_support.py")
        for forbidden in (
            "services",
            "dataplane",
            "workspace",
            "subprocess",
            "threading",
        ):
            self.assertNotIn(forbidden, imports)
        code = """
import sys
import backend.sandbox.sandbox_support
for name in (
	    "backend.sandbox.sandboxes",
	    "backend.workspace",
):
    if name in sys.modules:
        raise SystemExit(f"{name} loaded")
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(BACKEND_ROOT.parent)
        subprocess.run([sys.executable, "-c", code], check=True, env=env)

    def test_dataplane_package_init_is_import_light(self) -> None:
        # Importing the dataplane helper package should not pull in workspace
        # or service modules.
        imports = _top_level_import_segments(BACKEND_ROOT / "dataplane" / "__init__.py")
        for forbidden in ("worker", "workspace"):
            self.assertNotIn(forbidden, imports)
        code = """
import sys
import backend.dataplane
for name in (
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

    def test_project_overview_uses_direct_concrete_collaborators(self) -> None:
        # The project tool's current action has one consumer and one
        # implementation pair today.
        # Keep it lean by avoiding a single-impl reader port until a real
        # ControlApp composition gives that port a second implementation.
        imports = _import_segments(SERVICES_ROOT / "project_overview.py")
        self.assertIn("projects", imports)
        self.assertIn("reflections", imports)
        self.assertNotIn("project_readers", imports)
        source = (SERVICES_ROOT / "project_overview.py").read_text(encoding="utf-8")
        self.assertIn("projects: ProjectService", source)
        self.assertIn("reflections: ReflectionService", source)
        self.assertNotIn("class ProjectCurrentReader", source)
        self.assertNotIn("class SynthesisOverviewReader", source)
        from backend.services.project_overview import ProjectOverviewService
        from backend.services.projects import ProjectService
        from backend.services.reflections import ReflectionService

        hints = get_type_hints(ProjectOverviewService.__init__)
        self.assertIs(hints["projects"], ProjectService)
        self.assertIs(hints["reflections"], ReflectionService)

        tree = ast.parse(source)

        def collaborator_calls(name: str) -> set[str]:
            calls: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                owner = func.value
                if (
                    isinstance(owner, ast.Attribute)
                    and owner.attr == name
                    and isinstance(owner.value, ast.Name)
                    and owner.value.id == "self"
                ):
                    calls.add(func.attr)
            return calls

        self.assertEqual(collaborator_calls("projects"), {"current"})
        self.assertEqual(
            collaborator_calls("reflections"), {"latest_published", "open_reflection"}
        )

    def test_workflow_service_uses_reader_ports(self) -> None:
        # workflow.status_and_next is a control-safe orientation view. It
        # should compose against narrow read ports instead of redeclaring
        # collaborator protocols inside the workflow service module.
        imports = _import_segments(SERVICES_ROOT / "workflow.py")
        self.assertFalse(
            {"experiments", "reviews", "sandboxes", "reflections"} & imports
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
        imports = _import_segments(ARTIFACTS_ROOT / "resources.py")
        self.assertIn("resource_records", imports)
        self.assertNotIn("permissions", imports)
        source = (ARTIFACTS_ROOT / "resources.py").read_text(encoding="utf-8")
        self.assertIn("permissions: ResourceAssociationPolicy", source)
        self.assertNotIn("observer: ResourceObserver", source)
        self.assertNotIn("self.observer", source)
        self.assertNotIn("def register_file(", source)
        self.assertNotIn("class ResourceObserver", source)
        self.assertNotIn("class ResourceAssociationPolicy", source)
        proxy_source = (PLUGIN_ROOT / "mcp_server" / "local_data_plane.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('name == "resource.register"', proxy_source)
        self.assertIn("LocalResourceObserver", proxy_source)
        self.assertIn("observe_file(", proxy_source)

    def test_reflection_tools_uses_direct_concrete_collaborator(self) -> None:
        # reflection.* has one adapter and one implementation today. Keep it
        # lean by avoiding a single-impl port, but pin the narrow method surface
        # the adapter is allowed to use.
        imports = _import_segments(SERVICES_ROOT / "reflection_tools.py")
        self.assertIn("reflections", imports)
        self.assertNotIn("reflection_waves", imports)
        source = (SERVICES_ROOT / "reflection_tools.py").read_text(encoding="utf-8")
        self.assertIn("reflections: ReflectionService", source)
        self.assertNotIn("class ReflectionWaveStore", source)
        from backend.services.reflection_tools import ReflectionToolService
        from backend.services.reflections import ReflectionService

        self.assertIs(
            get_type_hints(ReflectionToolService.__init__)["reflections"],
            ReflectionService,
        )

        tree = ast.parse(source)
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        def enclosing_function(node: ast.AST) -> str | None:
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return parent.name
                parent = parents.get(parent)
            return None

        calls: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "self"
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "reflections"
            ):
                self.fail("reflection_tools must not dynamically access self.reflections")
            if not isinstance(node, ast.Attribute):
                continue
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                if node.attr == "reflections":
                    parent = parents.get(node)
                    if (
                        isinstance(parent, ast.Assign)
                        and node in parent.targets
                        and enclosing_function(node) == "__init__"
                    ):
                        continue
                    self.assertIsInstance(parent, ast.Attribute)
                    continue
            owner = node.value
            if not (
                isinstance(owner, ast.Attribute)
                and owner.attr == "reflections"
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "self"
            ):
                continue
            self.assertIn(
                node.attr, {"create", "get_state", "list_reflections", "transition"}
            )
            parent = parents.get(node)
            self.assertIsInstance(parent, ast.Call)
            self.assertIs(getattr(parent, "func", None), node)
            calls.add(node.attr)
        self.assertEqual(
            calls, {"create", "get_state", "list_reflections", "transition"}
        )

    def test_legacy_local_app_stack_is_removed(self) -> None:
        for rel in (
            "app.py",
            "local_runtime.py",
            "composition/local_mode.py",
            "dataplane/worker.py",
            "dataplane/tasks.py",
            "dataplane/state.py",
            "dataplane/sandbox_conn.py",
            "daemon/daemon_marker.py",
            "daemon/project_router.py",
            "daemon/import_tool.py",
        ):
            with self.subTest(rel=rel):
                self.assertFalse((BACKEND_ROOT / rel).exists())

    def test_control_app_uses_record_core_builder_for_record_services(self) -> None:
        app_source = (BACKEND_ROOT / "control" / "control_app.py").read_text(encoding="utf-8")
        record_source = (BACKEND_ROOT / "control" / "record_core.py").read_text(encoding="utf-8")

        self.assertIn("self.record_core = build_record_core", app_source)
        for service_ctor in (
            "ClaimService(",
            "ExperimentService(",
            "FeedService(",
            "GraphRefResolver(",
            "PermissionService(",
            "ProjectOverviewService(",
            "ProjectService(",
            "QuotaService(",
            "ResourceService(",
            "ReviewService(",
            "ReflectionService(",
        ):
            self.assertNotIn(service_ctor, app_source)
            self.assertIn(service_ctor, record_source)
        for forbidden in (
            "local_runtime",
            "dataplane",
            "workspace",
            "execution",
        ):
            self.assertNotIn(forbidden, _import_segments(BACKEND_ROOT / "control" / "record_core.py"))

    def test_control_app_does_not_build_local_runtime(self) -> None:
        source = (BACKEND_ROOT / "control" / "control_app.py").read_text(encoding="utf-8")
        self.assertIn("class ControlApp:", source)
        self.assertIn("build_record_core", source)
        self.assertIn("build_control_tool_handlers", source)
        self.assertIn("control_tool_names = set(CONTROL_PLANE_TOOL_NAMES)", source)
        self.assertIn("available_tool_names(storage_enabled=", source)
        self.assertIn("tool_names=control_tool_names", source)
        self.assertNotIn("class ControlActivitySink", source)
        self.assertNotIn("class ControlToolCallSink", source)
        self.assertNotIn("class ControlSandboxWorker", source)
        for forbidden in (
            "TestBrain",
            "build_local_runtime",
            "build_local_tool_handlers",
            "LocalDataPlaneWorker",
            "LocalWorkspace",
            "LocalFeedImageReader",
            "LocalResourceObserver",
            "ToolCallStore",
            "ActivityLogger",
        ):
            self.assertNotIn(forbidden, source)

    def test_control_mode_builds_control_app_not_local_app(self) -> None:
        path = BACKEND_ROOT / "composition" / "control_mode.py"
        source = path.read_text(encoding="utf-8")
        imports = _import_segments(path)
        self.assertIn("from ..control.control_app import ControlApp", source)
        self.assertIn("app = ControlApp(", source)
        self.assertNotIn("TestBrain", source)
        self.assertNotIn("build_local_runtime", source)
        self.assertIn("MountedMgmtKeyStore", source)
        self.assertIn("resolve_mgmt_key_path", source)
        self.assertIn("LocalMgmtKeyStore", source)
        self.assertIn("build_local_server", source)
        self.assertIn("CONTROL_COMPAT_REPO_ROOT", source)
        self.assertNotIn("tempfile", _import_segments(path))

    def test_management_key_store_is_adapter_not_service(self) -> None:
        # The service layer depends on the MgmtKeyStore port only. The local
        # filesystem key custody adapter belongs to composition-state wiring,
        # not services/.
        self.assertFalse((SERVICES_ROOT / "sandbox_mgmt_keys.py").exists())
        for path in sorted(SERVICES_ROOT.rglob("*.py")):
            with self.subTest(module=path.name):
                self.assertFalse(_imports_management_key_adapter(path))
                self.assertNotIn("LocalMgmtKeyStore", path.read_text(encoding="utf-8"))
        imports = _import_segments(BACKEND_ROOT / "sandbox" / "mgmt_keys.py")
        self.assertIn("ssh_keys", imports)
        self.assertNotIn("subprocess", imports)
        self.assertNotIn("services", imports)
        self.assertIn("LocalMgmtKeyStore", (BACKEND_ROOT / "composition" / "control_mode.py").read_text(encoding="utf-8"))
        self.assertNotIn(
            "subprocess",
            _import_segments(BACKEND_ROOT / "sandbox" / "managed_mgmt_keys.py"),
        )

    def test_local_ssh_keygen_is_single_sourced(self) -> None:
        self.assertEqual(
            _imports(BACKEND_ROOT / "sandbox" / "ssh_keys.py"),
            {"os", "pathlib", "subprocess", "utils"},
        )
        path = BACKEND_ROOT / "sandbox" / "mgmt_keys.py"
        self.assertIn("ssh_keys", _import_segments(path))
        self.assertNotIn("subprocess", _import_segments(path))
        self.assertNotIn("ssh-keygen", path.read_text(encoding="utf-8"))

    def test_control_app_import_keeps_local_io_modules_unloaded(self) -> None:
        # Importing the unified brain should not pull in local workspace or
        # data-plane worker machinery.
        code = """
import sys
import backend.control.control_app
for name in (
    "backend.workspace",
    "backend.sandbox.mgmt_keys",
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

    # The proxy package, its stdlib-only shared helper, and the standard library
    # are the only allowed roots. (sys.stdlib_module_names covers 3.11+.)
    def test_mcp_server_imports_only_stdlib(self) -> None:
        import sys

        plugin_root = BACKEND_ROOT.parent
        mcp_root = plugin_root / "mcp_server"
        shared_root = plugin_root / "research_plugin_shared"
        own = (
            {p.stem for p in mcp_root.glob("*.py")}
            | {p.stem for p in shared_root.glob("*.py")}
            | {
                "mcp_server",
                "research_plugin_shared",
            }
        )
        allowed = set(sys.stdlib_module_names) | own | {"__future__"}
        for path in sorted([*mcp_root.glob("*.py"), *shared_root.glob("*.py")]):
            with self.subTest(module=path.name):
                external = _imports(path) - allowed
                self.assertFalse(
                    external,
                    f"{path.name} imports non-stdlib modules: {sorted(external)}",
                )

    def test_mcp_server_dynamic_imports_stay_in_backend(self) -> None:
        # Static imports are pinned above, but the proxy also lazy-imports
        # backend modules by string (importlib.import_module / _import_attr).
        # Pin every dynamic target to the backend package so a third-party
        # module can never sneak in through the dynamic path — and remember:
        # any new dynamic target must either be pydantic-free or sit behind an
        # ImportError fallback (tests/surface/test_static_tool_catalog.py
        # drives the proxy with pydantic blocked to prove it).
        plugin_root = BACKEND_ROOT.parent
        targets: set[str] = set()
        for path in sorted((plugin_root / "mcp_server").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                is_import_module = (
                    isinstance(func, ast.Attribute) and func.attr == "import_module"
                )
                is_import_attr = isinstance(func, ast.Name) and func.id == "_import_attr"
                first = node.args[0]
                if (is_import_module or is_import_attr) and isinstance(first, ast.Constant):
                    targets.add(str(first.value))
        self.assertTrue(targets, "expected the proxy's lazy backend imports to be found")
        for target in sorted(targets):
            self.assertEqual(
                target.split(".")[0],
                "backend",
                f"dynamic import of {target!r} — the proxy may lazy-import "
                "only backend modules",
            )

    def test_mcp_server_ships_the_static_tool_catalog(self) -> None:
        # The bare-python fallback for tools/list: the file must ship with the
        # tree and carry the routing fields the proxy reads. Freshness against
        # the live contracts is pinned by test_static_tool_catalog.py.
        catalog_path = BACKEND_ROOT.parent / "mcp_server" / "_tool_catalog.json"
        self.assertTrue(catalog_path.is_file())
        tools = json.loads(catalog_path.read_text(encoding="utf-8"))["tools"]
        self.assertTrue(tools)
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("inputSchema", tool)
            self.assertIn("plane", tool)

    def test_mcp_server_does_not_require_datetime_utc_alias(self) -> None:
        # Codex may launch the stdio proxy with Apple CLT Python 3.9.
        # datetime.UTC was added in 3.11, so proxy modules must use
        # datetime.timezone.utc instead.
        plugin_root = BACKEND_ROOT.parent
        for path in sorted((plugin_root / "mcp_server").glob("*.py")):
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("from datetime import UTC", source)
                self.assertNotIn("datetime.UTC", source)


if __name__ == "__main__":
    unittest.main()
