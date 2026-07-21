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

from merv.brain.surface.tools.contracts import (
    CONTROL_PLANE_TOOL_NAMES,
    DATA_PLANE_TOOL_NAMES,
    TOOL_CONTRACTS,
    TOOL_PLANE_REGISTRY,
)
from tests.paths import (
    ARTIFACTS_ROOT,
    BACKEND_ROOT,
    CLIENT_ROOT,
    DOMAIN_ROOT,
    FEED_ROOT,
    IMPORT_ROOT,
    PORTS_ROOT,
    PROXY_ROOT,
    RESEARCH_CORE_ROOT,
    SERVICES_ROOT,
    SHARED_ROOT,
    SURFACE_ROOT,
)


# Service-shaped glue that must remain cloud-safe and process-free.
GLUE_SERVICE_FILES = (
    *(SERVICES_ROOT / name for name in ("auth.py", "identity.py", "permissions.py")),
    BACKEND_ROOT / "application" / "maintenance.py",
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
    BACKEND_ROOT / "sandbox" / "sandbox_paths.py",
    SURFACE_ROOT / "tools" / "tool_facade.py",
    SURFACE_ROOT / "tools" / "tool_handlers.py",
    RESEARCH_CORE_ROOT / "projects.py",
    RESEARCH_CORE_ROOT / "claims.py",
    RESEARCH_CORE_ROOT / "experiments.py",
    RESEARCH_CORE_ROOT / "reflections.py",
    RESEARCH_CORE_ROOT / "reviews.py",
    BACKEND_ROOT / "application" / "status_guidance.py",
    RESEARCH_CORE_ROOT / "snapshots.py",
    BACKEND_ROOT / "application" / "experiments" / "presentation.py",
    SERVICES_ROOT / "permissions.py",
    BACKEND_ROOT / "application" / "workflow.py",
    BACKEND_ROOT / "sandbox" / "facade.py",
    FEED_ROOT / "feed.py",
    FEED_ROOT / "feed_policy.py",
    BACKEND_ROOT / "sandbox" / "sandbox_metrics.py",
    SURFACE_ROOT / "control" / "record_core.py",
    SURFACE_ROOT / "control" / "control_app.py",
    SURFACE_ROOT / "control" / "control_runtime.py",
    SURFACE_ROOT / "control" / "control_client.py",
    BACKEND_ROOT / "kernel" / "state" / "store.py",
    BACKEND_ROOT / "kernel" / "state" / "dialects.py",
    BACKEND_ROOT / "sandbox" / "managed_mgmt_keys.py",
)

# Module names (any dotted segment) control modules may never import.
CONTROL_FORBIDDEN_SEGMENTS = {
    "dataplane",
    "proxy",
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


def _class_method_names(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name for item in node.body if isinstance(item, ast.FunctionDef)
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


def _all_import_targets(path: Path, *, package: str, root: Path) -> set[str]:
    """Resolve ordinary imports plus literal dynamic-import targets."""
    rel = path.relative_to(root)
    file_package = (*package.split("."), *rel.parent.parts)
    targets: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            if node.level:
                base_parts = file_package[: len(file_package) - (node.level - 1)]
                base = ".".join(base_parts)
                if node.module:
                    base = f"{base}.{node.module}"
            elif node.module:
                base = node.module
            else:
                continue
            targets.add(base)
            targets.update(
                f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
            )
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            is_import_module = (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            is_import_attr = isinstance(func, ast.Name) and func.id == "_import_attr"
            first = node.args[0]
            if (
                (is_import_module or is_import_attr)
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                targets.add(first.value)
    return targets


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
            if name == "importlib.import_module" and "subprocess" in _literal_args(
                node
            ):
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
        from merv.brain.surface.transport.http_policy import HTTP_DATA_PLANE_FEATURE_TO_TOOL

        self.assertEqual(
            HTTP_DATA_PLANE_FEATURE_TO_TOOL,
            {
                "resource_registration": "resource.register",
                "resource_association": "resource.register",
            },
        )
        self.assertLessEqual(
            set(HTTP_DATA_PLANE_FEATURE_TO_TOOL.values()), DATA_PLANE_TOOL_NAMES
        )

    def test_proxy_local_data_plane_dispatches_contract_plane_sets(self) -> None:
        proxy = PROXY_ROOT / "proxy.py"
        local_data_plane = PROXY_ROOT / "local_data_plane.py"
        source = proxy.read_text(encoding="utf-8")

        manifest = json.loads(
            (PROXY_ROOT / "_tool_manifest.json").read_text(encoding="utf-8")
        )["tools"]
        self.assertEqual(
            {tool["name"] for tool in manifest},
            set(TOOL_CONTRACTS),
        )
        local = {
            tool["name"]
            for tool in manifest
            if tool["plane"] == "data"
            or tool["executionStrategy"] == "control-plus-local-enrichment"
        }
        self.assertEqual(local, DATA_PLANE_TOOL_NAMES | {"sandbox.get", "sandbox.health"})
        self.assertIn("_PROXY_MANIFEST_PATH", source)
        self.assertNotIn("merv.brain.surface.tools.contracts", source)
        self.assertIn("local_handler_identity(name)", local_data_plane.read_text(encoding="utf-8"))
        self.assertFalse(
            _method_name_literal_branches(
                local_data_plane,
                class_name="LocalDataPlane",
                method_name="call_tool",
            )
        )

    def test_proxy_responsibilities_stay_split_and_composition_stays_small(self) -> None:
        proxy = (PROXY_ROOT / "proxy.py").read_text(encoding="utf-8")
        shell = (PROXY_ROOT / "mcp_shell.py").read_text(encoding="utf-8")
        gateway = (PROXY_ROOT / "tool_gateway.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(proxy.splitlines()), 100)
        self.assertLessEqual(len(shell.splitlines()), 120)
        self.assertLessEqual(len(gateway.splitlines()), 350)
        for leaked in (
            "def _call_tool(",
            "def _send(",
            "def _refresh_login_session(",
            "def _connect_project(",
        ):
            self.assertNotIn(leaked, proxy)


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
            "import merv.brain.sandbox.mgmt_keys\n",
            "from merv.brain.sandbox import mgmt_keys\n",
            "from ..sandbox.mgmt_keys import LocalMgmtKeyStore\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            for index, source in enumerate(cases):
                path = Path(tmp) / f"service_{index}.py"
                path.write_text(source, encoding="utf-8")
                with self.subTest(source=source.strip()):
                    self.assertTrue(_imports_management_key_adapter(path))

    def test_only_sandbox_io_modules_spawn_processes(self) -> None:
        # Everything service-shaped is spawn-free; inside the sandbox module
        # only execution/ (provider IO) and ssh_keys.py (keygen) may spawn.
        sandbox_record_modules = [
            path
            for path in (BACKEND_ROOT / "sandbox").glob("*.py")
            if path.name != "ssh_keys.py"
        ]
        for path in sorted(
            (
                *GLUE_SERVICE_FILES,
                *RESEARCH_CORE_ROOT.rglob("*.py"),
                *FEED_ROOT.rglob("*.py"),
                *sandbox_record_modules,
            )
        ):
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
        from merv.brain.surface.tools.tool_facade import ToolDispatcher, ToolPermissionPolicy

        hints = get_type_hints(ToolDispatcher.__init__)
        self.assertIs(hints["permissions"], ToolPermissionPolicy)
        self.assertIn(Protocol, ToolPermissionPolicy.__mro__)
        path = SURFACE_ROOT / "tools" / "tool_facade.py"
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
        source = (BACKEND_ROOT / "kernel" / "state" / "store.py").read_text(
            encoding="utf-8"
        )
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
                source = (BACKEND_ROOT / "kernel" / "state" / name).read_text(
                    encoding="utf-8"
                )
                self.assertNotIn(
                    "store", _imports(BACKEND_ROOT / "kernel" / "state" / name)
                )
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
import merv.brain.sandbox.sandbox_support
loaded = sorted(
    name for name in sys.modules
    if name == "merv.proxy" or name.startswith("merv.proxy.")
)
if loaded:
    raise SystemExit("brain import loaded proxy modules: " + ", ".join(loaded))
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(IMPORT_ROOT)
        subprocess.run([sys.executable, "-c", code], check=True, env=env)

    def test_brain_checkout_modules_are_absent(self) -> None:
        self.assertFalse((BACKEND_ROOT / "dataplane").exists())
        self.assertFalse((BACKEND_ROOT / "workspace.py").exists())

    def test_workflow_reads_have_explicit_application_boundaries(self) -> None:
        source = (BACKEND_ROOT / "application" / "workflow.py").read_text(
            encoding="utf-8"
        )
        imports = _import_segments(BACKEND_ROOT / "application" / "workflow.py")
        self.assertIn("facade", imports)
        self.assertNotIn("from ..research_core.experiments", source)
        self.assertNotIn("reviews", imports)
        self.assertNotIn("sandboxes", imports)
        self.assertIn("snapshots: ResearchSnapshots", source)
        self.assertIn("sandboxes: SandboxReads", source)
        self.assertIn("policy: StatusGuidancePolicy", source)
        for obsolete in ("workflow.py", "workflow_views.py", "project_overview.py"):
            self.assertFalse((RESEARCH_CORE_ROOT / obsolete).exists())
        self.assertFalse((RESEARCH_CORE_ROOT / "next_action.py").exists())

    def test_resource_service_owns_association_policy(self) -> None:
        # ResourceService is the control-safe record half. Local observation
        # belongs to the local composition edge, not to the record service.
        imports = _import_segments(ARTIFACTS_ROOT / "resources.py")
        self.assertIn("association_policy", imports)
        self.assertNotIn("permissions", imports)
        source = (ARTIFACTS_ROOT / "resources.py").read_text(encoding="utf-8")
        self.assertNotIn("permissions:", source)
        self.assertNotIn("self.permissions", source)
        self.assertNotIn("observer: ResourceObserver", source)
        self.assertNotIn("self.observer", source)
        self.assertNotIn("def register_file(", source)
        self.assertNotIn("class ResourceObserver", source)
        proxy_source = (PROXY_ROOT / "local_data_plane.py").read_text(encoding="utf-8")
        contracts_source = (SERVICES_ROOT / "tools" / "contracts.py").read_text(encoding="utf-8")
        self.assertIn('handler_identity="local.register_resource"', contracts_source)
        self.assertIn("def _register_resource(", proxy_source)
        self.assertIn("LocalResourceObserver", proxy_source)
        self.assertIn("observe_file(", proxy_source)

    def test_reflection_tools_present_research_facts_in_application(self) -> None:
        self.assertFalse((RESEARCH_CORE_ROOT / "reflection_tools.py").exists())
        facade = (RESEARCH_CORE_ROOT / "facade.py").read_text(encoding="utf-8")
        presentation = (BACKEND_ROOT / "application" / "reflections.py").read_text(
            encoding="utf-8"
        )
        app = (SURFACE_ROOT / "control" / "control_app.py").read_text(
            encoding="utf-8"
        )
        record = (SURFACE_ROOT / "control" / "record_core.py").read_text(
            encoding="utf-8"
        )
        for method in (
            "create_reflection",
            "reflection_state",
            "list_reflections",
            "transition_reflection",
        ):
            self.assertIn(f"    def {method}(", facade)
        for method in ("create", "get", "list", "transition"):
            self.assertIn(f"    def {method}(", presentation)
        self.assertIn("ReflectionCommands(reflections=self.research_core)", app)
        self.assertIn("reflection_tools=self.reflection_commands", app)
        self.assertNotIn("reflection_tools", record)

    def test_legacy_local_app_stack_is_removed(self) -> None:
        for rel in (
            "app.py",
            "local_runtime.py",
            "composition/local_mode.py",
            "surface/composition/local_mode.py",
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
        app_source = (SURFACE_ROOT / "control" / "control_app.py").read_text(
            encoding="utf-8"
        )
        record_source = (SURFACE_ROOT / "control" / "record_core.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("self.record_core = build_record_core", app_source)
        for service_ctor in (
            "ClaimService(",
            "ExperimentService(",
            "FeedService(",
            "GraphRefResolver(",
            "PermissionService(",
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
            self.assertNotIn(
                forbidden, _import_segments(SURFACE_ROOT / "control" / "record_core.py")
            )

    def test_control_app_does_not_build_local_runtime(self) -> None:
        source = (SURFACE_ROOT / "control" / "control_app.py").read_text(
            encoding="utf-8"
        )
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
        path = SURFACE_ROOT / "composition" / "control_mode.py"
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
        service_modules = (
            *GLUE_SERVICE_FILES,
            *RESEARCH_CORE_ROOT.rglob("*.py"),
            *FEED_ROOT.rglob("*.py"),
            *(
                path
                for path in (BACKEND_ROOT / "sandbox").glob("*.py")
                if path.name not in {"mgmt_keys.py", "managed_mgmt_keys.py"}
            ),
        )
        for path in sorted(service_modules):
            with self.subTest(module=path.name):
                self.assertFalse(_imports_management_key_adapter(path))
                self.assertNotIn("LocalMgmtKeyStore", path.read_text(encoding="utf-8"))
        imports = _import_segments(BACKEND_ROOT / "sandbox" / "mgmt_keys.py")
        self.assertIn("ssh_keys", imports)
        self.assertNotIn("subprocess", imports)
        self.assertNotIn("services", imports)
        self.assertIn(
            "LocalMgmtKeyStore",
            (SURFACE_ROOT / "composition" / "control_mode.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertNotIn(
            "subprocess",
            _import_segments(BACKEND_ROOT / "sandbox" / "managed_mgmt_keys.py"),
        )

    def test_local_ssh_keygen_is_single_sourced(self) -> None:
        self.assertEqual(
            _imports(BACKEND_ROOT / "sandbox" / "ssh_keys.py"),
            {"os", "pathlib", "subprocess", "kernel"},
        )
        path = BACKEND_ROOT / "sandbox" / "mgmt_keys.py"
        self.assertIn("ssh_keys", _import_segments(path))
        self.assertNotIn("subprocess", _import_segments(path))
        self.assertNotIn("ssh-keygen", path.read_text(encoding="utf-8"))

    def test_control_app_import_keeps_local_io_modules_unloaded(self) -> None:
        # Importing the unified brain must never cross into the client plane.
        code = """
import sys
import merv.brain.surface.control.control_app
loaded = sorted(
    name for name in sys.modules
    if name == "merv.proxy" or name.startswith("merv.proxy.")
)
if loaded:
    raise SystemExit("brain import loaded proxy modules: " + ", ".join(loaded))
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(IMPORT_ROOT)
        subprocess.run([sys.executable, "-c", code], check=True, env=env)


class ProxyStdlibOnlyTest(unittest.TestCase):
    """The stdio MCP proxy must stay stdlib-only (cloud plan Phase 8 packaging).

    The proxy ships as part of the base wheel with no third-party deps so it
    runs under any Python — even the daemon profile that drops the provider
    SDKs. Walk the merv.proxy package and assert no import resolves outside the
    stdlib (or the package itself), so the dual-upstream rewrite never reaches
    for pydantic/fastapi/boto3 by accident.
    """

    # The proxy package, its stdlib-only shared helper, and the standard library
    # are the only allowed roots — merv.brain must never be imported statically.
    # (sys.stdlib_module_names covers 3.11+.)
    def test_proxy_imports_only_stdlib(self) -> None:
        import sys

        for package, root in (
            ("merv.proxy", PROXY_ROOT),
            ("merv.shared", SHARED_ROOT),
            ("merv.client", CLIENT_ROOT),
        ):
            allowed_prefixes = {
                "merv.proxy": ("merv.proxy", "merv.shared"),
                "merv.shared": ("merv.shared",),
                "merv.client": ("merv.client", "merv.shared"),
            }[package]
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                # The file's own package: subpackage-aware, so nested modules
                # resolve relative imports against the right base.
                file_pkg = ".".join((package, *path.relative_to(root).parent.parts))
                names: set[str] = set()
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        if node.module == "__future__":
                            continue
                        if node.level:
                            # Resolve relative imports against the owning package.
                            pkg_parts = file_pkg.split(".")
                            base = ".".join(
                                pkg_parts[: len(pkg_parts) - (node.level - 1)]
                            )
                            names.add(f"{base}.{node.module}" if node.module else base)
                        elif node.module:
                            names.add(node.module)
                with self.subTest(module=path.relative_to(root).as_posix()):
                    external = {
                        name
                        for name in names
                        if name.split(".")[0] not in sys.stdlib_module_names
                        and not any(
                            name == prefix or name.startswith(prefix + ".")
                            for prefix in allowed_prefixes
                        )
                    }
                    self.assertFalse(
                        external,
                        f"{path.name} imports non-stdlib modules: {sorted(external)}",
                    )

    def test_client_package_performs_no_brain_imports(self) -> None:
        # The login CLI ships in the slim client bundle: like the proxy, its
        # closure must stay merv.shared + stdlib — never merv.brain.
        from tests.paths import CLIENT_ROOT

        for path in sorted(CLIENT_ROOT.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            targets = _all_import_targets(path, package="merv.client", root=CLIENT_ROOT)
            offenders = {
                t for t in targets if t == "merv.brain" or t.startswith("merv.brain.")
            }
            with self.subTest(module=path.relative_to(CLIENT_ROOT).as_posix()):
                self.assertFalse(offenders, f"merv.client imports brain: {sorted(offenders)}")

    def test_proxy_performs_no_brain_imports_static_or_dynamic(self) -> None:
        targets: set[str] = set()
        for package, root in (
            ("merv.proxy", PROXY_ROOT),
            ("merv.shared", SHARED_ROOT),
            ("merv.client", CLIENT_ROOT),
        ):
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" not in path.parts:
                    targets.update(
                        _all_import_targets(path, package=package, root=root)
                    )
        brain_targets = sorted(
            target
            for target in targets
            if target == "merv.brain" or target.startswith("merv.brain.")
        )
        self.assertFalse(brain_targets, f"client plane imports brain: {brain_targets}")

    def test_brain_performs_no_proxy_imports_static_or_dynamic(self) -> None:
        targets: set[str] = set()
        for path in sorted(BACKEND_ROOT.rglob("*.py")):
            if "__pycache__" not in path.parts:
                targets.update(
                    _all_import_targets(path, package="merv.brain", root=BACKEND_ROOT)
                )
        proxy_targets = sorted(
            target
            for target in targets
            if target == "merv.proxy" or target.startswith("merv.proxy.")
        )
        self.assertFalse(proxy_targets, f"brain imports proxy: {proxy_targets}")

    def test_proxy_and_shared_runtime_closure_excludes_brain(self) -> None:
        modules: list[str] = []
        for package, root in (
            ("merv.proxy", PROXY_ROOT),
            ("merv.shared", SHARED_ROOT),
            ("merv.client", CLIENT_ROOT),
        ):
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                parts = [
                    part
                    for part in path.relative_to(root).with_suffix("").parts
                    if part != "__init__"
                ]
                modules.append(".".join((package, *parts)))
        code = f"""
import importlib
import sys
import tempfile
from pathlib import Path

class BlockBrain:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "merv.brain" or fullname.startswith("merv.brain."):
            raise ImportError("blocked brain import: " + fullname)
        return None

sys.meta_path.insert(0, BlockBrain())
for name in {sorted(modules)!r}:
    importlib.import_module(name)
from merv.proxy.proxy import HttpProxyMcpServer, ProxyConfig
with tempfile.TemporaryDirectory() as tmp:
    server = HttpProxyMcpServer(
        config=ProxyConfig(repo_root=Path(tmp), control_url="http://127.0.0.1:1")
    )
    initialized = server.handle({{"jsonrpc": "2.0", "id": 1, "method": "initialize"}})
    listed = server.handle({{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}})
    assert initialized and "result" in initialized, initialized
    assert listed and "result" in listed and listed["result"]["tools"], listed
loaded = sorted(
    name for name in sys.modules
    if name == "merv.brain" or name.startswith("merv.brain.")
)
if loaded:
    raise SystemExit("brain modules loaded: " + ", ".join(loaded))
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(IMPORT_ROOT)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_no_stale_moved_module_paths_in_executable_tree(self) -> None:
        forbidden = (
            "merv.brain." + "dataplane",
            "merv.brain." + "workspace",
            "merv.brain.object_storage." + "file_transfer",
            "merv.brain.object_storage." + "storage_guidance",
            "merv.brain.feed." + "feed_embeds",
            "merv.brain.feed." + "feed_images",
            "merv.brain.artifacts." + "markdown_images",
            "merv.brain.artifacts." + "roles",
            "merv/brain/" + "dataplane",
            "merv/brain/" + "workspace.py",
            "merv/brain/object_storage/" + "file_transfer.py",
            "merv/brain/object_storage/" + "storage_guidance.py",
            "merv/brain/feed/" + "feed_embeds.py",
            "merv/brain/feed/" + "feed_images.py",
            "merv/brain/artifacts/" + "markdown_images.py",
            "merv/brain/artifacts/" + "roles.py",
            # T6 surface fold: the old brain-root strays and merv.client's
            # former home must never be referenced again.
            "merv.brain." + "tools",
            "merv.brain." + "transport",
            "merv.brain." + "composition",
            "merv.brain." + "control",
            "merv.brain." + "services",
            "merv.brain." + "config",
            "merv.brain." + "observability",
            "merv.brain." + "client_cli",
            "merv/brain/" + "tools/",
            "merv/brain/" + "transport/",
            "merv/brain/" + "composition/",
            "merv/brain/" + "control/",
            "merv/brain/" + "services/",
            "merv/brain/" + "config.py",
            "merv/brain/" + "observability.py",
            "merv/brain/" + "client_cli.py",
        )
        roots = (
            IMPORT_ROOT,
            IMPORT_ROOT.parent / "tests",
            IMPORT_ROOT.parent / "scripts",
            IMPORT_ROOT.parent / "bin",
            IMPORT_ROOT.parent / "deploy",
            IMPORT_ROOT.parent / "clients",
            IMPORT_ROOT.parent / "docs",
        )
        stale: list[str] = []
        for root in roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                try:
                    source = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for target in forbidden:
                    if target in source:
                        stale.append(
                            f"{path.relative_to(IMPORT_ROOT.parent)}: {target}"
                        )
        self.assertFalse(stale, "stale moved paths: " + ", ".join(stale))

    def test_proxy_ships_the_static_tool_catalog(self) -> None:
        # The bare-python fallback for tools/list: the file must ship with the
        # tree and carry the routing fields the proxy reads. Freshness against
        # the live contracts is pinned by test_static_tool_catalog.py.
        catalog_path = PROXY_ROOT / "_tool_catalog.json"
        self.assertTrue(catalog_path.is_file())
        tools = json.loads(catalog_path.read_text(encoding="utf-8"))["tools"]
        self.assertTrue(tools)
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("inputSchema", tool)
            self.assertIn("plane", tool)
        manifest_path = PROXY_ROOT / "_tool_manifest.json"
        self.assertTrue(manifest_path.is_file())
        routed = json.loads(manifest_path.read_text(encoding="utf-8"))["tools"]
        self.assertEqual({tool["name"] for tool in routed}, set(TOOL_CONTRACTS))
        for tool in routed:
            self.assertIn("executionStrategy", tool)
            self.assertIn("scopeStrategy", tool)
            self.assertIn("handlerIdentity", tool)
        package_config = (IMPORT_ROOT.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('"_tool_catalog.json", "_tool_manifest.json"', package_config)

    def test_proxy_does_not_require_datetime_utc_alias(self) -> None:
        # Codex may launch the stdio proxy with Apple CLT Python 3.9.
        # datetime.UTC was added in 3.11, so proxy modules must use
        # datetime.timezone.utc instead. merv.shared is statically imported by
        # the proxy, so it lives under the same floor.
        for package, root in (
            ("merv.proxy", PROXY_ROOT),
            ("merv.shared", SHARED_ROOT),
            ("merv.client", CLIENT_ROOT),
        ):
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                with self.subTest(
                    module=f"{package}:{path.relative_to(root).as_posix()}"
                ):
                    source = path.read_text(encoding="utf-8")
                    self.assertNotIn("from datetime import UTC", source)
                    self.assertNotIn("datetime.UTC", source)

    def test_proxy_avoids_runtime_pep604_unions(self) -> None:
        # Same Apple CLT Python 3.9 target as the datetime.UTC pin: `X | Y`
        # on types raises TypeError at runtime before 3.10. Annotations are
        # safe only under the lazy-annotations future import; runtime
        # positions (type aliases, defaults) must spell typing.Optional.
        # The static net is the unambiguous None-operand shape — set/dict/int
        # `|` stays legal — and the system-python import test below is the
        # authoritative backstop for anything it cannot see.
        for package, root in (
            ("merv.proxy", PROXY_ROOT),
            ("merv.shared", SHARED_ROOT),
            ("merv.client", CLIENT_ROOT),
        ):
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                lazy_annotations = any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "__future__"
                    and any(alias.name == "annotations" for alias in node.names)
                    for node in tree.body
                )
                annotation_unions: set[int] = set()
                for node in ast.walk(tree):
                    anns: list[ast.expr] = []
                    if isinstance(node, ast.AnnAssign):
                        anns.append(node.annotation)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.returns is not None:
                            anns.append(node.returns)
                    elif isinstance(node, ast.arg) and node.annotation is not None:
                        anns.append(node.annotation)
                    for ann in anns:
                        annotation_unions.update(
                            id(sub)
                            for sub in ast.walk(ann)
                            if isinstance(sub, ast.BinOp)
                            and isinstance(sub.op, ast.BitOr)
                        )
                violations: list[str] = []
                for node in ast.walk(tree):
                    if not (
                        isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
                    ):
                        continue
                    if id(node) in annotation_unions:
                        if not lazy_annotations:
                            violations.append(
                                f"line {node.lineno}: annotation union needs "
                                "`from __future__ import annotations`"
                            )
                    elif any(
                        isinstance(side, ast.Constant) and side.value is None
                        for side in (node.left, node.right)
                    ):
                        violations.append(
                            f"line {node.lineno}: runtime `X | None` union — "
                            "use typing.Optional"
                        )
                with self.subTest(
                    module=f"{package}:{path.relative_to(root).as_posix()}"
                ):
                    self.assertFalse(violations, "; ".join(violations))

    def test_proxy_tree_imports_under_system_python(self) -> None:
        # The authoritative floor check: on macOS dev machines /usr/bin/python3
        # is Apple CLT 3.9 — the very interpreter agent clients launch the
        # proxy with. When a pre-3.11 system python exists, prove every
        # merv.proxy and merv.shared module imports under it. (__main__ is
        # included: its entrypoint sits behind an import guard.)
        interpreter = Path("/usr/bin/python3")
        if not interpreter.exists():
            self.skipTest("no system python3 to test against")
        probe = subprocess.run(
            [str(interpreter), "-c", "import sys; print(*sys.version_info[:2])"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            self.skipTest("system python3 is not runnable (CLT stub)")
        version = tuple(int(part) for part in probe.stdout.split())
        if version >= (3, 11):
            self.skipTest(
                f"system python3 is {version[0]}.{version[1]}; not below the packaged floor"
            )
        modules: list[str] = []
        for package, root in (
            ("merv.proxy", PROXY_ROOT),
            ("merv.shared", SHARED_ROOT),
            ("merv.client", CLIENT_ROOT),
        ):
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                parts = [
                    p
                    for p in path.relative_to(root).with_suffix("").parts
                    if p != "__init__"
                ]
                modules.append(".".join((package, *parts)))
        code = (
            "import importlib, sys\n"
            f"failures = []\n"
            f"for name in {sorted(modules)!r}:\n"
            "    try:\n"
            "        importlib.import_module(name)\n"
            "    except Exception as exc:\n"
            "        failures.append(name + ': ' + repr(exc))\n"
            "sys.exit('\\n'.join(failures) if failures else 0)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(IMPORT_ROOT)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [str(interpreter), "-c", code], capture_output=True, text=True, env=env
        )
        self.assertEqual(
            result.returncode,
            0,
            f"proxy tree failed to import under system python "
            f"{version[0]}.{version[1]}:\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
