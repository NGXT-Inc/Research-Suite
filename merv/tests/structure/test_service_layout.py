from __future__ import annotations

import ast
import re
import unittest
from collections import Counter
from inspect import Parameter, signature as inspect_signature
from pathlib import Path
from typing import Any, Protocol, get_type_hints, is_typeddict

from tests.paths import (
    ARTIFACTS_ROOT,
    BACKEND_ROOT,
    DOMAIN_ROOT,
    FEED_ROOT,
    PLUGIN_ROOT,
    PORTS_ROOT,
    PROXY_ROOT,
    RESEARCH_CORE_ROOT,
    SERVICES_ROOT,
    SURFACE_ROOT,
)

ROOT = PLUGIN_ROOT
SERVICES = SERVICES_ROOT

GLUE_SERVICE_FILES = (
    *(SERVICES_ROOT / name for name in ("auth.py", "identity.py", "permissions.py")),
    BACKEND_ROOT / "application" / "maintenance.py",
)
RESEARCH_CORE = RESEARCH_CORE_ROOT
RESEARCH_CORE_DOMAIN = RESEARCH_CORE / "domain"
UI_SRC = PLUGIN_ROOT.parent / "research_state_ui" / "src"
HTTP_TRANSPORT_MODULES = (
    SURFACE_ROOT / "transport" / "admin_http.py",
    SURFACE_ROOT / "transport" / "data_plane_http.py",
    SURFACE_ROOT / "transport" / "feed_http.py",
    SURFACE_ROOT / "transport" / "http_api.py",
    SURFACE_ROOT / "transport" / "mcp_http.py",
    *sorted((SURFACE_ROOT / "transport" / "api").glob("*.py")),
)
HTTP_API_APP = SURFACE_ROOT / "transport" / "api" / "app.py"
HTTP_API_GATEWAY = SURFACE_ROOT / "transport" / "api" / "gateway.py"
HTTP_API_VIEWS = SURFACE_ROOT / "transport" / "api" / "views.py"
HTTP_API_PACKAGE = SURFACE_ROOT / "transport" / "api"

_CONTROL_APP_SCAN_EXCLUSIONS = {
    "config.py",
    "control/control_app.py",
    "control/record_core.py",
    "transport/http_server.py",
}
CONTROL_APP_SCAN_MODULES = tuple(
    path
    for path in sorted(SURFACE_ROOT.rglob("*.py"))
    if not path.relative_to(SURFACE_ROOT).as_posix().startswith("composition/")
    and path.relative_to(SURFACE_ROOT).as_posix() not in _CONTROL_APP_SCAN_EXCLUSIONS
)

# Exact, line-independent debt ledgers for the remaining whole-ControlApp HTTP
# seams. Counter keys deliberately identify a file and top-level collaborator,
# not a line number, so harmless formatting does not churn the baseline. Both
# ledgers are shrinking: a new entry is a regression, while a removed entry
# fails with an instruction to delete the now-stale baseline debt.
RAW_CONTROL_APP_ACCESS_BASELINE: Counter[tuple[str, str]] = Counter()
WHOLE_CONTROL_APP_CARRIER_BASELINE: Counter[tuple[str, str]] = Counter()

_RAW_CONTROL_APP_COLLABORATORS = {
    "artifacts",
    "experiments",
    "feed",
    "projects",
    "resources",
    "reviews",
    "sandboxes",
    "storage",
    "store",
    "tool_calls",
}


def _source(name: str) -> str:
    return (SERVICES / name).read_text(encoding="utf-8")


def _sandbox_source(name: str) -> str:
    return (BACKEND_ROOT / "sandbox" / name).read_text(encoding="utf-8")


def _rc_source(name: str) -> str:
    return (RESEARCH_CORE / name).read_text(encoding="utf-8")


def _api_app_source() -> str:
    return HTTP_API_APP.read_text(encoding="utf-8")


def _http_gateway_source() -> str:
    return HTTP_API_GATEWAY.read_text(encoding="utf-8")


def _api_views_source() -> str:
    return HTTP_API_VIEWS.read_text(encoding="utf-8")


def _api_package_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(HTTP_API_PACKAGE.glob("*.py"))
    )


def _artifacts_source(name: str) -> str:
    return (ARTIFACTS_ROOT / name).read_text(encoding="utf-8")


def _import_modules(name: str) -> set[str]:
    return {module.split(".", 1)[0] for module in _import_module_names(SERVICES / name)}


def _rc_import_modules(name: str) -> set[str]:
    return {
        module.split(".", 1)[0] for module in _import_module_names(RESEARCH_CORE / name)
    }


def _import_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "__future__":
                continue
            modules.add(node.module)
    return modules


def _import_segments(path: Path) -> set[str]:
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


def _strict_self_collaborator_call_names(
    source: str, collaborator: str, allowed_calls: set[str]
) -> set[str]:
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
            and node.args[1].value == collaborator
        ):
            raise AssertionError(f"must not dynamically access self.{collaborator}")
        if not isinstance(node, ast.Attribute):
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            if node.attr != collaborator:
                continue
            parent = parents.get(node)
            if (
                isinstance(parent, ast.Assign)
                and node in parent.targets
                and enclosing_function(node) == "__init__"
            ):
                continue
            if not isinstance(parent, ast.Attribute):
                raise AssertionError(
                    f"self.{collaborator} must only be used for direct method calls"
                )
            continue
        owner = node.value
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == collaborator
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            if node.attr not in allowed_calls:
                raise AssertionError(
                    f"unexpected self.{collaborator}.{node.attr} access"
                )
            parent = parents.get(node)
            if not isinstance(parent, ast.Call) or parent.func is not node:
                raise AssertionError(
                    f"self.{collaborator}.{node.attr} must be called directly"
                )
            calls.add(node.attr)
    return calls


def _assigned_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    def collect(target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                collect(item)

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            collect(target)
    return names


def _attribute_chain(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        owner = _attribute_chain(node.value)
        return (*owner, node.attr) if owner is not None else None
    return None


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _surface_relative(path: Path) -> str:
    return path.relative_to(SURFACE_ROOT).as_posix()


def _whole_app_locals(tree: ast.AST) -> set[str]:
    """Names assigned a whole app, including one-hop local aliases."""
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if not _is_whole_app_receiver(node.value, local_names=names):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _is_whole_app_receiver(node: ast.AST, *, local_names: set[str]) -> bool:
    chain = _attribute_chain(node)
    if chain in {
        ("api", "app"),
        ("ctx", "api", "app"),
        ("self", "app"),
        ("self", "backend"),
    }:
        return True
    if isinstance(node, ast.Name) and node.id in local_names:
        return True
    return isinstance(node, ast.Call) and _call_name(node) in {
        "app_for",
        "app_for_project",
    }


def _raw_control_app_accesses() -> Counter[tuple[str, str]]:
    accesses: Counter[tuple[str, str]] = Counter()
    for path in CONTROL_APP_SCAN_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        local_names = _whole_app_locals(tree)
        relative = _surface_relative(path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _RAW_CONTROL_APP_COLLABORATORS
                and _is_whole_app_receiver(node.value, local_names=local_names)
            ):
                accesses[(relative, node.attr)] += 1
    return accesses


def _whole_control_app_carriers() -> Counter[tuple[str, str]]:
    carriers: Counter[tuple[str, str]] = Counter()
    for path in CONTROL_APP_SCAN_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = _surface_relative(path)
        local_names = _whole_app_locals(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                call_name = _call_name(node) or "<call>"
                if call_name in {"app_for", "app_for_project"}:
                    carriers[(relative, f"{call_name}(...)")] += 1
                for keyword in node.keywords:
                    if not (
                        _is_whole_app_receiver(
                            keyword.value, local_names=local_names
                        )
                        or isinstance(keyword.value, ast.Name)
                        and keyword.value.id == "app"
                    ):
                        continue
                    value = ast.unparse(keyword.value)
                    expression = f"{call_name}({keyword.arg or '**'}={value})"
                    if call_name == "ToolInvocationGateway":
                        expression = f"backend={value}"
                    carriers[(relative, expression)] += 1
                for argument in node.args:
                    if _is_whole_app_receiver(argument, local_names=local_names):
                        carriers[(relative, f"{call_name}({ast.unparse(argument)})")] += 1
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if any(
                    _attribute_chain(target) == ("self", "app")
                    for target in targets
                ):
                    carriers[(relative, f"self.app={ast.unparse(node.value)}")] += 1
                elif _attribute_chain(node.value) in {
                    ("api", "app"),
                    ("ctx", "api", "app"),
                    ("self", "app"),
                    ("self", "backend"),
                }:
                    for target in targets:
                        carriers[
                            (relative, f"{ast.unparse(target)}={ast.unparse(node.value)}")
                        ] += 1
            elif isinstance(node, ast.Return):
                chain = _attribute_chain(node.value) if node.value is not None else None
                if chain in {
                    ("api", "app"),
                    ("ctx", "api", "app"),
                    ("self", "app"),
                    ("self", "backend"),
                }:
                    carriers[(relative, f"return {'.'.join(chain)}")] += 1
    return carriers


def _format_counter(counter: Counter[tuple[str, str]]) -> str:
    return ", ".join(
        f"{path}: {name} x{count}"
        for (path, name), count in sorted(counter.items())
    )


VOCABULARY_NAMES = {
    "CLAIM_CONFIDENCES",
    "CLAIM_STATUSES",
    "EXPERIMENT_ACTIVE_PROCESS_STATUSES",
    "EXPERIMENT_TERMINAL_STATUSES",
    "GATED_ROLES",
    "GATED_ROLE_BYTE_CAPS",
    "LEGACY_PROJECT_GRAPH_ROLE",
    "LEGACY_PROPOSALS_ROLE",
    "LEGACY_REFLECTION_DOC_ROLE",
    "LEGACY_REFLECTION_LENS_DOC_ROLE",
    "LEGACY_RESOURCE_ROLES",
    "PROJECT_GRAPH_ROLE",
    "PROJECT_GRAPH_ROLES",
    "REFLECTION_LENS_DOC_ROLE",
    "REFLECTION_LENS_DOC_ROLES",
    "RESOURCE_ROLES",
    "RESOURCE_TARGET_TYPES",
    "REVIEW_ROLES",
    "REVIEW_VERDICT_VALUES",
    "REVIEW_VERDICTS",
}

LOCAL_FS_IMPORTS = {"os", "pathlib", "shutil"}

DOMAIN_FORBIDDEN_SEGMENTS = {
    "composition",
    "dataplane",
    "execution",
    "services",
    "state",
    "workspace",
}


class ServiceLayoutTest(unittest.TestCase):
    def test_experiment_service_keeps_lint_and_agent_projection_out(self) -> None:
        source = _rc_source("experiments.py")

        self.assertNotIn("def slim_experiment_state", source)
        self.assertNotIn(
            "experiment_views", _import_segments(RESEARCH_CORE / "experiments.py")
        )
        self.assertNotIn("def get_state_agent", source)
        self.assertNotIn("def list_experiments_agent", source)
        self.assertNotIn("def report_problems", source)
        self.assertNotIn("def plan_sections_missing", source)
        self.assertNotIn("REQUIRED_PLAN_SECTIONS", source)
        self.assertNotIn("_HEADING_RE", source)

    def test_record_services_do_not_create_local_workspaces(self) -> None:
        for name in ("experiments.py", "reflections.py"):
            with self.subTest(module=name):
                source = _rc_source(name)
                import_modules = _import_module_names(RESEARCH_CORE / name)
                self.assertNotIn("ensure_workspace", source)
                self.assertNotIn("_ensure_workspace", source)
                self.assertNotIn("reflection_policy", import_modules)
                self.assertFalse(
                    _rc_import_modules(name) & LOCAL_FS_IMPORTS,
                    f"{name} should not import local filesystem helpers",
                )
                self.assertNotIn(".mkdir(", source)
                self.assertNotIn("open(", source)

    def test_workflow_policy_is_pure_and_query_uses_public_reads(self) -> None:
        policy_path = BACKEND_ROOT / "application" / "status_guidance.py"
        policy = policy_path.read_text(encoding="utf-8")
        policy_imports = _import_segments(policy_path)
        self.assertIn("class StatusGuidancePolicy:", policy)
        self.assertNotIn(".execute(", policy)
        self.assertNotIn("BaseStateStore", policy)
        self.assertNotIn("domain", policy_imports)
        self.assertIn("facade", policy_imports)
        self.assertFalse((RESEARCH_CORE / "next_action.py").exists())

        query = (BACKEND_ROOT / "application" / "workflow.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("class StatusAndNextQuery:", query)
        self.assertIn("class ProjectDashboardQuery:", query)
        self.assertIn("snapshots: ResearchSnapshots", query)
        self.assertIn("sandboxes: SandboxReads", query)
        self.assertIn("from .ports.sandbox import SandboxReads", query)
        self.assertNotIn("from ..sandbox.facade import SandboxReads", query)

        reader = _rc_source("snapshots.py")
        self.assertIn("class ResearchSnapshotReader:", reader)
        self.assertEqual(reader.count("self.experiments.get_state"), 1)
        self.assertNotIn("sandbox", _import_segments(RESEARCH_CORE / "snapshots.py"))

    def test_artifact_lint_is_domain_leaf_module(self) -> None:
        # Pure text lint: regexes, a callback type, and shared domain markdown
        # image parsing. No filesystem imports — figure resolution is the
        # caller's business (submission capture).
        self.assertEqual(
            _import_module_names(RESEARCH_CORE_DOMAIN / "artifacts.py"),
            {"re", "collections.abc", "merv.shared.markdown_images"},
        )

    def test_evidence_contract_is_artifacts_port(self) -> None:
        self.assertEqual(
            _import_module_names(ARTIFACTS_ROOT / "ports" / "evidence.py"),
            {"dataclasses", "typing"},
        )

    def test_http_policy_is_fastapi_free(self) -> None:
        imports = _import_module_names(SURFACE_ROOT / "transport" / "http_policy.py")

        self.assertEqual(imports, {"dataclasses"})

    def test_ports_are_neutral_and_outside_services(self) -> None:
        expected_imports = {
            "mgmt_keys.py": {"pathlib", "typing"},
            "quota_admission.py": {"dataclasses", "typing"},
            "resource_records.py": {"typing", "merv.shared.resource_records"},
            "sandbox_lifecycle.py": {"datetime", "typing"},
            "sandbox_worker.py": {"pathlib", "typing"},
            "reflection_writers.py": {"typing"},
            "task_channel.py": {"typing"},
        }
        for name, allowed_imports in expected_imports.items():
            with self.subTest(module=name):
                self.assertFalse((SERVICES / name).exists())
                self.assertTrue((PORTS_ROOT / name).exists())
                self.assertEqual(
                    _import_module_names(PORTS_ROOT / name),
                    allowed_imports,
                )
                source = (PORTS_ROOT / name).read_text(encoding="utf-8")
                for forbidden in ("httpx", "sqlite3", "json", "tempfile", "os."):
                    self.assertNotIn(forbidden, source)
        self.assertFalse((PORTS_ROOT / "project_readers.py").exists())
        self.assertFalse((PORTS_ROOT / "reflection_waves.py").exists())
        self.assertFalse((PORTS_ROOT / "review_targets.py").exists())
        self.assertFalse((PORTS_ROOT / "workflow_readers.py").exists())
        resource_record_path = PORTS_ROOT / "resource_records.py"
        resource_record_source = resource_record_path.read_text(encoding="utf-8")
        self.assertNotIn("class ResourceObservation", resource_record_source)
        self.assertIn(
            "from merv.shared.resource_records import ResourceObservation",
            resource_record_source,
        )
        self.assertIn("class ResourceObserver", resource_record_source)
        self.assertNotIn("class ResourceAssociationPolicy", resource_record_source)
        self.assertEqual(
            _class_method_names(resource_record_path, "ResourceObserver"),
            {"observe_file"},
        )
        from merv.brain.kernel.ports.resource_records import (
            ResourceObservation,
            ResourceObserver,
        )
        from merv.shared.resource_records import (
            ResourceObservation as SharedResourceObservation,
        )

        self.assertTrue(is_typeddict(ResourceObservation))
        self.assertIs(ResourceObservation, SharedResourceObservation)
        self.assertEqual(ResourceObservation.__module__, "merv.shared.resource_records")
        self.assertEqual(
            set(get_type_hints(ResourceObservation)),
            {
                "path",
                "kind",
                "title",
                "created_by",
                "mtime_ns",
                "ctime_ns",
                "size_bytes",
                "content_sha256",
                "content_type",
            },
        )
        self.assertIs(
            get_type_hints(ResourceObserver.observe_file)["return"],
            ResourceObservation,
        )
        reflection_writer_path = PORTS_ROOT / "reflection_writers.py"
        self.assertEqual(
            _class_method_names(reflection_writer_path, "ReflectionClaimWriter"),
            {"create_from_reflection", "update_from_reflection"},
        )
        self.assertEqual(
            _class_method_names(reflection_writer_path, "ReflectionExperimentWriter"),
            {"create_from_reflection"},
        )
        from merv.brain.kernel.ports.reflection_writers import (
            ReflectionClaimWriter,
            ReflectionExperimentWriter,
        )

        self.assertIn(Protocol, ReflectionClaimWriter.__mro__)
        self.assertIn(Protocol, ReflectionExperimentWriter.__mro__)

    def test_sandbox_lifecycle_workers_use_ports_not_concrete_services(self) -> None:
        self.assertNotIn(
            "experiments",
            _import_segments(BACKEND_ROOT / "sandbox" / "sandbox_provisioner.py"),
        )
        self.assertNotIn(
            "sandbox_mgmt_keys",
            _import_segments(BACKEND_ROOT / "sandbox" / "facade.py"),
        )
        self.assertFalse((SERVICES / "sandbox_mgmt_keys.py").exists())
        self.assertNotIn("class QuotaAdmission", _sandbox_source("facade.py"))
        self.assertNotIn(
            "class ControlPlaneView", _sandbox_source("sandbox_daemons.py")
        )
        self.assertNotIn(
            "class SyncSessionIssuer", _sandbox_source("sandbox_provisioner.py")
        )
        daemon_imports = _import_segments(
            BACKEND_ROOT / "sandbox" / "sandbox_daemons.py"
        )
        self.assertNotIn("experiments", daemon_imports)
        self.assertNotIn("sandbox_provisioner", daemon_imports)

    def test_auto_sync_poller_is_removed(self) -> None:
        local_source = _sandbox_source("sandbox_daemons.py")
        http_source = _api_package_source()
        api_source = (UI_SRC / "api.js").read_text(encoding="utf-8")
        components = UI_SRC / "components"

        self.assertFalse((BACKEND_ROOT / "sandbox" / "sandbox_autosync.py").exists())
        self.assertFalse((components / "ExperimentSyncIndicator.jsx").exists())
        self.assertFalse((components / "ExperimentSyncDetailsModal.jsx").exists())
        self.assertNotIn("run_auto_sync_target", local_source)
        self.assertNotIn("_auto_sync_loop", local_source)
        self.assertNotIn("auto_sync_thread", local_source)
        self.assertNotIn("RESEARCH_PLUGIN_SANDBOX_AUTO_RSYNC", local_source)
        self.assertNotIn("RESEARCH_PLUGIN_SANDBOX_RSYNC_INTERVAL", local_source)
        for source in (http_source, api_source):
            self.assertNotIn("/sandbox/sync", source)
            self.assertNotIn("syncSandbox", source)
        for path in components.glob("*.jsx"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("sandbox.rsynced", source)
            self.assertNotIn("sandbox.synced", source)
            self.assertNotIn("sandbox.rsync_error", source)
            self.assertNotIn("initial_rsynchronized", source)

    def test_resource_service_records_observations_without_local_observer(self) -> None:
        source = _artifacts_source("resources.py")
        imports = _import_segments(ARTIFACTS_ROOT / "resources.py")

        self.assertNotIn("resource_records", imports)
        self.assertIn("def record_observation(", source)
        self.assertNotIn("observer: ResourceObserver", source)
        self.assertNotIn("self.observer", source)
        self.assertNotIn("observe_file", source)
        self.assertNotIn("def register_file(", source)
        self.assertNotIn("def _register_one(", source)
        self.assertNotIn("_resolve_repo_file", source)
        self.assertNotIn("def _content_sha256(", source)
        self.assertNotIn("file_path.stat(", source)

    def test_resource_association_uses_submitted_artifact_bytes(self) -> None:
        source = _artifacts_source("resources.py")
        start = source.index("    def associate(")
        end = source.index("    def associate_observed(")
        associate_slice = source[start:end]

        self.assertIn("self.associate_observed", associate_slice)
        self.assertNotIn("_resolve_repo_file", source)
        self.assertNotIn("_ensure_current_version_for_resource", source)
        self.assertNotIn("_capture_gated_blob", source)

    def test_resource_service_has_no_local_file_reads(self) -> None:
        source = _artifacts_source("resources.py")

        self.assertNotIn(".read_bytes(", source)
        self.assertNotIn("repo_root", source)
        self.assertNotIn("self.workspace", source)
        self.assertNotIn("backfill_gated_blobs", source)

    def test_resource_service_owns_association_policy(self) -> None:
        source = _artifacts_source("resources.py")
        imports = _import_segments(ARTIFACTS_ROOT / "resources.py")

        self.assertNotIn("permissions", imports)
        self.assertIn("association_policy", imports)
        self.assertNotIn("permissions:", source)
        self.assertNotIn("self.permissions", source)
        self.assertIn("validate_resource_association(", source)

        from merv.brain.artifacts.resources import ResourceService

        get_type_hints(ResourceService.__init__)

    def test_review_service_owns_vocabulary_validation(self) -> None:
        imports = _import_segments(RESEARCH_CORE / "reviews.py")
        self.assertNotIn("permissions", imports)
        self.assertIn(
            "kernel.identity", _import_module_names(RESEARCH_CORE / "reviews.py")
        )
        self.assertIn("domain", imports)
        self.assertIn("review_validation", imports)
        source = _rc_source("reviews.py")
        self.assertNotIn("permissions:", source)
        self.assertNotIn("self.permissions", source)
        self.assertIn("validate_review_role(role=role)", source)
        self.assertIn("validate_review_verdict(verdict=verdict)", source)

    def test_review_return_policy_is_domain_leaf_module(self) -> None:
        self.assertEqual(
            _import_module_names(RESEARCH_CORE_DOMAIN / "review_returns.py"),
            {"dataclasses"},
        )
        imports = _import_module_names(RESEARCH_CORE / "reviews.py")
        source = _rc_source("reviews.py")

        self.assertIn("domain.review_returns", imports)
        self.assertIn("resolve_review_return", source)
        self.assertNotIn("experiment-attempt-review rejections must set", source)
        self.assertNotIn("project-reflection-review rejections must set", source)
        self.assertNotIn("experiment-design-review rejections cannot return_to", source)

    def test_review_gate_policy_is_domain_leaf_module(self) -> None:
        self.assertEqual(
            _import_module_names(RESEARCH_CORE_DOMAIN / "review_gates.py"), set()
        )
        imports = _import_module_names(RESEARCH_CORE / "reviews.py")
        source = _rc_source("reviews.py")

        self.assertIn("domain.review_gates", imports)
        self.assertIn("is_review_gate_exempt", source)
        self.assertIn("gate.review.role", source)
        self.assertNotIn('"synthesis_review":', source)
        self.assertNotIn('"design_review":', source)
        self.assertNotIn('"experiment_review":', source)

    def test_review_service_uses_direct_concrete_targets(self) -> None:
        imports = _import_segments(RESEARCH_CORE / "reviews.py")

        self.assertIn("experiments", imports)
        self.assertIn("reflections", imports)
        self.assertNotIn("review_targets", imports)
        source = _rc_source("reviews.py")
        self.assertIn("experiments: ExperimentService", source)
        self.assertIn("reflections: ReflectionService", source)
        self.assertNotIn("class ExperimentReviewTarget", source)
        self.assertNotIn("class SynthesisReviewTarget", source)
        from merv.brain.research_core.experiments import ExperimentService
        from merv.brain.research_core.domain.review_handoff import (
            reviewer_handoff_payload,
        )
        from merv.brain.research_core.domain.review_snapshot import snapshot_from_id
        from merv.brain.research_core.reviews import ReviewService
        from merv.brain.research_core.reflections import ReflectionService

        hints = get_type_hints(ReviewService.__init__)
        self.assertIs(hints["experiments"], ExperimentService)
        self.assertIs(hints["reflections"], ReflectionService)
        self.assertIs(ReviewService.reviewer_handoff, reviewer_handoff_payload)
        self.assertIs(ReviewService.snapshot_from_id, snapshot_from_id)
        experiment_calls = {
            "get_state_with_gate",
            "send_back_to_planned",
            "send_back_to_running",
            "target_snapshot_id",
        }
        reflection_calls = {
            "get_state_with_gate",
            "send_back_to_reflecting",
            "send_back_to_synthesizing",
            "target_snapshot_id",
        }
        self.assertEqual(
            _strict_self_collaborator_call_names(
                source, "experiments", experiment_calls
            ),
            experiment_calls,
        )
        self.assertEqual(
            _strict_self_collaborator_call_names(
                source, "reflections", reflection_calls
            ),
            reflection_calls,
        )

    def test_feed_service_does_not_read_local_image_paths(self) -> None:
        source = (FEED_ROOT / "feed.py").read_text(encoding="utf-8")

        self.assertIn("from . import feed_policy", source)
        self.assertNotIn("resolve_repo_relative_file", source)
        self.assertNotIn(".read_bytes(", source)
        self.assertNotIn("workspace", source)
        self.assertIn("post_observed", source)

    def test_feed_policy_is_domain_leaf_module(self) -> None:
        self.assertEqual(_import_module_names(FEED_ROOT / "feed_policy.py"), set())

    def test_experiment_names_are_domain_policy(self) -> None:
        self.assertEqual(
            _import_module_names(RESEARCH_CORE_DOMAIN / "experiment_names.py"),
            {"re", "kernel.utils"},
        )
        for name in ("experiments.py", "reflections.py"):
            with self.subTest(module=name):
                imports = _import_module_names(RESEARCH_CORE / name)
                self.assertIn("domain.experiment_names", imports)
                self.assertNotIn("experiment_names", imports)

    def test_reflection_tools_are_exposed_by_the_research_facade(self) -> None:
        self.assertFalse((DOMAIN_ROOT / "reflection_projection.py").exists())
        self.assertFalse((RESEARCH_CORE / "reflection_tools.py").exists())
        facade = _rc_source("facade.py")
        for method in (
            "create_reflection",
            "reflection_state",
            "list_reflections",
            "transition_reflection",
        ):
            self.assertIn(f"    def {method}(", facade)

    def test_graph_lint_is_domain_leaf_module(self) -> None:
        self.assertEqual(
            _import_module_names(RESEARCH_CORE_DOMAIN / "graph_lint.py"), {"json"}
        )

    def test_domain_modules_do_not_import_backend_layers(self) -> None:
        for path in sorted(DOMAIN_ROOT.glob("*.py")):
            if path.name == "__init__.py":
                continue
            with self.subTest(module=path.name):
                segments = {
                    segment
                    for module in _import_module_names(path)
                    for segment in module.split(".")
                }
                forbidden = segments & DOMAIN_FORBIDDEN_SEGMENTS
                self.assertFalse(
                    forbidden,
                    f"domain modules must stay independent of backend layers: {sorted(forbidden)}",
                )

    def test_utils_stays_free_of_local_path_guards(self) -> None:
        path = BACKEND_ROOT / "kernel" / "utils.py"
        self.assertEqual(
            _import_module_names(path),
            {"datetime", "uuid", "merv.shared.errors", "merv.shared.path_utils"},
        )
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("resolve_repo_relative_file", source)
        self.assertNotIn("pathlib", source)
        self.assertNotIn("os.path", source)

        repo_paths = PROXY_ROOT / "dataplane" / "repo_paths.py"
        # merv.shared.project_dirs is the stdlib-only single owner
        # of the checkout state-dir names the guard excludes.
        self.assertEqual(
            _import_module_names(repo_paths),
            {
                "pathlib",
                "typing",
                "merv.shared.errors",
                "merv.shared.project_dirs",
            },
        )
        repo_path_source = repo_paths.read_text(encoding="utf-8")
        self.assertIn("def resolve_repo_path", repo_path_source)
        self.assertIn("def repo_relative_path", repo_path_source)

    def test_kernel_error_reexports_preserve_shared_identity(self) -> None:
        from merv.brain.kernel import utils as kernel_utils
        from merv.shared import errors as shared_errors

        for name in (
            "ResearchPluginError",
            "NotFoundError",
            "PermissionDeniedError",
            "ValidationError",
            "WorkflowError",
            "ContentUnavailableError",
            "DataPlaneRequiredError",
        ):
            with self.subTest(error=name):
                self.assertIs(getattr(kernel_utils, name), getattr(shared_errors, name))

    def test_kernel_path_helper_reexport_preserves_shared_identity(self) -> None:
        from merv.brain.kernel.utils import safe_experiment_dirname as kernel_helper
        from merv.shared.path_utils import safe_experiment_dirname as shared_helper

        self.assertIs(kernel_helper, shared_helper)

    def test_resource_observation_port_reexport_preserves_shared_identity(self) -> None:
        from merv.brain.kernel.ports.resource_records import (
            ResourceObservation as PortObservation,
        )
        from merv.shared.resource_records import (
            ResourceObservation as SharedObservation,
        )

        self.assertIs(PortObservation, SharedObservation)
        self.assertEqual(PortObservation.__module__, "merv.shared.resource_records")

    def test_iso_parsing_is_single_sourced(self) -> None:
        for path in sorted(BACKEND_ROOT.rglob("*.py")):
            if path.name == "utils.py":
                continue
            with self.subTest(module=path.relative_to(BACKEND_ROOT).as_posix()):
                self.assertNotIn(
                    "fromisoformat",
                    path.read_text(encoding="utf-8"),
                )

    def test_iso_formatting_is_single_sourced(self) -> None:
        for path in sorted(BACKEND_ROOT.rglob("*.py")):
            if path.name == "utils.py":
                continue
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.relative_to(BACKEND_ROOT).as_posix()):
                self.assertNotIn('replace("+00:00", "Z")', source)
                self.assertNotIn("replace('+00:00', 'Z')", source)
                self.assertIsNone(
                    re.search(r"datetime\.now\([^)]*UTC[^)]*\)\.isoformat\(", source)
                )

    def test_env_coercion_is_single_sourced(self) -> None:
        # logging is allowed for the one-per-process legacy-env deprecation
        # warning; the kernel resolver must otherwise stay dependency-free.
        self.assertEqual(
            _import_module_names(BACKEND_ROOT / "kernel" / "env.py"),
            {"collections.abc", "logging", "os"},
        )
        for path in sorted(BACKEND_ROOT.rglob("*.py")):
            if path.name == "env.py":
                continue
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.relative_to(BACKEND_ROOT).as_posix()):
                self.assertNotIn("def env_flag", source)
                self.assertNotIn("def env_float", source)
                self.assertNotIn(
                    'RESEARCH_PLUGIN_ACTIVITY_STDERR", "").lower()', source
                )
                self.assertNotIn(
                    'RESEARCH_PLUGIN_SANDBOX_REAPER", "1").lower()', source
                )
                self.assertNotIn(
                    'RESEARCH_PLUGIN_SANDBOX_AUTO_RSYNC", "1").lower()',
                    source,
                )

    def test_modal_integer_env_parsing_uses_shared_helper(self) -> None:
        source = (
            BACKEND_ROOT / "sandbox" / "execution" / "backends" / "modal" / "config.py"
        ).read_text(encoding="utf-8")

        self.assertIn("from .....kernel.env import env_int", source)
        self.assertNotIn("def _env_int", source)
        self.assertNotIn("def _env_non_negative_int", source)
        self.assertNotIn("_positive_int(os.environ.get", source)
        self.assertIn("_modal_env_int(", source)
        self.assertIn("_positive_env_int(", source)
        self.assertIn("_non_negative_env_int(", source)

    def test_services_type_against_base_state_store(self) -> None:
        concrete_store_names = {"StateStore", "SqliteStateStore"}
        sandbox_record_modules = [
            path
            for path in (BACKEND_ROOT / "sandbox").glob("*.py")
            if path.name != "__init__.py"
        ]
        for path in sorted(
            (
                *GLUE_SERVICE_FILES,
                *RESEARCH_CORE.rglob("*.py"),
                *FEED_ROOT.rglob("*.py"),
                *sandbox_record_modules,
            )
        ):
            if path.name == "__init__.py":
                continue
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            module = alias.name.split(".")
                            self.assertFalse(
                                module[-2:] == ["state", "store"]
                                or module[-1:] == ["state"],
                                "services should not import concrete state modules",
                            )
                        continue
                    if isinstance(node, ast.ImportFrom):
                        imported = {alias.name for alias in node.names}
                        module = node.module.split(".") if node.module else []
                        if "state" in imported and (
                            not module or module[-1] in {"merv", "brain", "kernel"}
                        ):
                            self.fail(
                                "services should not import the state package directly"
                            )
                        if not node.module:
                            continue
                        module = node.module.split(".")
                        if not (
                            module[-2:] == ["state", "store"]
                            or module[-1:] == ["state"]
                        ):
                            continue
                        self.assertNotIn(
                            "*",
                            imported,
                            "services should not star-import state modules",
                        )
                        self.assertNotIn(
                            "store",
                            imported,
                            "services should not import the concrete store module",
                        )
                        concrete = concrete_store_names & imported
                        self.assertFalse(
                            concrete,
                            "services should type store dependencies against BaseStateStore",
                        )

    def test_store_contract_uses_neutral_connection_types(self) -> None:
        source = (BACKEND_ROOT / "kernel" / "state" / "store.py").read_text(
            encoding="utf-8"
        )
        base_source = source[
            source.index("class BaseStateStore:") : source.index("class StateStore(")
        ]
        self.assertIn("class Row(Protocol)", source)
        self.assertIn("class ResultCursor(Protocol)", source)
        self.assertIn("class Connection(Protocol)", source)
        self.assertIn("def connect(self) -> Connection:", base_source)
        self.assertIn("def transaction(self) -> Iterator[Connection]:", base_source)
        self.assertIn("parameters: Sequence[Any] = ()", source)
        self.assertIn("def __enter__(self) -> Connection:", source)
        self.assertIn("tb: TracebackType | None", source)
        self.assertNotIn("sqlite3.", base_source)
        self.assertIn("def next_created_seq(*, conn: Connection", source)
        self.assertIn("row: Row | Mapping[str, Any] | None", source)

        from merv.brain.kernel.state.store import Connection, ResultCursor, Row

        for protocol in (Row, ResultCursor, Connection):
            self.assertIn(Protocol, protocol.__mro__)

    def test_control_services_do_not_leak_sqlite_connection_types(self) -> None:
        for path in (
            ARTIFACTS_ROOT / "resources.py",
            BACKEND_ROOT / "sandbox" / "facade.py",
        ):
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("sqlite3.Connection", source)
                self.assertNotIn("sqlite3.Row", source)
                self.assertNotIn("import sqlite3", source)

    def test_transport_uses_contract_capabilities_for_sandbox_lifecycle_specials(
        self,
    ) -> None:
        source = _http_gateway_source()
        contracts_source = (SURFACE_ROOT / "tools" / "contracts.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('name == "sandbox.get"', source)
        self.assertNotIn('name != "sandbox.get"', source)
        self.assertNotIn('name == "sandbox.release"', source)
        self.assertIn("TOOL_MANIFEST.get(name)", source)
        self.assertIn("contract.hosted_control_sandbox_lookup", source)
        self.assertIn("hosted_control_sandbox_lookup=True", contracts_source)
        marker = "if (\n            self.surface.hosted_control\n            and contract is not None\n            and contract.hosted_control_sandbox_lookup"
        start = source.index(marker)
        end = source.index("return self.tools.call_tool", start)
        block = source[start:end]
        self.assertIn("tenant_id=None", block)
        self.assertIn("self.sandboxes.get", block)
        self.assertIn("include_data_plane_enrichment=False", block)
        self.assertNotIn(".store.transaction", block)
        self.assertNotIn("require_project_id", block)

    def test_http_surface_policy_keeps_mode_decisions_named(self) -> None:
        source = _api_app_source()
        policy_source = (SURFACE_ROOT / "transport" / "http_policy.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("class _HttpSurfacePolicy", source)
        self.assertIn("surface_policy: HttpSurfacePolicy | None = None", source)
        self.assertIn(
            "surface = surface_policy or HttpSurfacePolicy.for_surface(", source
        )
        control_source = (SURFACE_ROOT / "composition" / "control_mode.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("surface_policy=surface", control_source)
        for decision in (
            "CONTROL_RESTRICT_CORS_ENV_VAR",
            "hosted_control=True",
        ):
            with self.subTest(decision=decision):
                self.assertIn(decision, control_source)
        for removed_decision in (
            "CONTROL_REQUIRE_AUTH_ENV_VAR",
            "expose_local_data_plane",
            "accept_repo_root_context",
            "allow_data_plane_tool_calls",
            "require_bearer_auth",
            "require_privileged_bearer_auth",
            "enforce_project_scope",
        ):
            with self.subTest(removed_decision=removed_decision):
                self.assertNotIn(removed_decision, control_source)
        control_builder = control_source[
            control_source.index("def _control_http_surface(") :
        ]
        self.assertNotIn("auth is not None", control_builder)
        self.assertNotIn("auth is None", control_builder)
        self.assertIn("class HttpSurfacePolicy", policy_source)
        self.assertIn("def for_surface(", policy_source)
        self.assertNotIn("for_auth_present", source)
        self.assertNotIn("for_auth_present", policy_source)
        self.assertNotIn("auth_required", source)
        for field_name in (
            "restrict_cors",
            "hosted_control",
            "allow_data_plane_http",
            "use_hosted_tool_policies",
        ):
            with self.subTest(field_name=field_name):
                self.assertIn(field_name, policy_source)
        self.assertNotIn("require_bearer_auth", policy_source)
        self.assertNotIn("require_privileged_bearer_auth", policy_source)
        self.assertNotIn("enforce_project_scope", policy_source)

    def test_http_transport_does_not_carry_interim_project_scope_gate(self) -> None:
        source = _api_package_source()

        self.assertNotIn(".store.require_project_id(", source)
        self.assertNotIn(".store.transaction(", source)
        self.assertNotIn("def require_project_scope(", source)
        self.assertNotIn("target.app.projects.require_project_scope(", source)
        self.assertNotIn("project_ids_for_tenant", source)

    def test_hosted_tool_call_metadata_uses_policy_table(self) -> None:
        source = _http_gateway_source()
        policy_source = (SURFACE_ROOT / "transport" / "http_policy.py").read_text(
            encoding="utf-8"
        )
        from merv.brain.surface.transport.http_policy import HOSTED_CONTROL_TOOL_POLICIES

        self.assertEqual(
            set(HOSTED_CONTROL_TOOL_POLICIES),
            {"project", "project.list", "review.start"},
        )
        self.assertTrue(
            HOSTED_CONTROL_TOOL_POLICIES["review.start"].telemetry_from_review_request
        )
        self.assertNotIn("tenant_id_fallback", policy_source)
        self.assertNotIn("class _HostedToolPolicy", source)
        self.assertIn("HOSTED_CONTROL_TOOL_POLICIES", source)
        self.assertIn("HOSTED_CONTROL_TOOL_POLICIES", policy_source)
        for tool_name in (
            "project",
            "project.list",
            "review.start",
        ):
            self.assertIn(f'"{tool_name}": HostedToolPolicy', policy_source)
            self.assertNotIn(
                f'if surface.hosted_control and name == "{tool_name}"', source
            )
        self.assertIn("telemetry_from_review_request=True", policy_source)
        self.assertIn("self.reviews.request_project_id(", source)
        self.assertNotIn("SELECT project_id FROM review_requests", source)

    def test_http_data_plane_capabilities_use_policy_table(self) -> None:
        source = _api_app_source()
        route_source = _api_package_source()
        policy_source = (SURFACE_ROOT / "transport" / "http_policy.py").read_text(
            encoding="utf-8"
        )
        from merv.brain.surface.transport.http_policy import HTTP_DATA_PLANE_FEATURE_TO_TOOL

        self.assertEqual(
            HTTP_DATA_PLANE_FEATURE_TO_TOOL,
            {
                "resource_registration": "resource.register",
                "resource_association": "resource.register",
            },
        )
        self.assertIn("HTTP_DATA_PLANE_FEATURE_TO_TOOL", policy_source)
        self.assertIn("surface.data_plane_http_capabilities()", route_source)
        self.assertNotIn("require_data_plane_for_http", route_source)
        self.assertNotIn("require_data_plane_for_http", source)
        for feature, tool_name in HTTP_DATA_PLANE_FEATURE_TO_TOOL.items():
            with self.subTest(feature=feature):
                self.assertNotIn(f'feature="{feature}"', route_source)
                self.assertNotIn(f'tool="{tool_name}"', route_source)
                self.assertNotIn(
                    f'"{feature}": surface.allow_data_plane_http',
                    route_source,
                )

    def test_admin_http_routes_are_lifted_out_of_main_factory(self) -> None:
        source = _api_app_source()
        admin_source = (SURFACE_ROOT / "transport" / "admin_http.py").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            _import_module_names(SURFACE_ROOT / "transport" / "admin_http.py"),
            {"typing"},
        )
        self.assertIn("register_admin_routes(", source)
        self.assertIn('"/api/admin/cleanup"', admin_source)
        self.assertIn('"/api/admin/tenants/{tenant_id}/counters"', admin_source)
        self.assertNotIn('"/api/admin/cleanup"', source)
        self.assertNotIn('"/api/admin/tenants/{tenant_id}/counters"', source)
        self.assertNotIn("TenantCounters", source)
        self.assertIn(
            "tenant_counters=tenant_counters or api.tenant_counters",
            source,
        )
        self.assertNotIn("app=api.app", source)
        self.assertNotIn("app.", admin_source)
        self.assertIn("cleanup.run_all().as_dict()", admin_source)
        self.assertIn("tenant_counters(tenant_id=tenant_id)", admin_source)
        self.assertNotIn("TenantCounters", admin_source)
        self.assertNotIn("require_admin", admin_source)
        self.assertNotIn("require_tenant_or_admin", admin_source)

    def test_mcp_http_routes_are_shared_by_local_and_control(self) -> None:
        source = _api_app_source()
        mcp_source = (SURFACE_ROOT / "transport" / "mcp_http.py").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            _import_module_names(SURFACE_ROOT / "transport" / "mcp_http.py"),
            {
                "collections.abc",
                "json",
                "typing",
                "fastapi",
                "fastapi.concurrency",
                "kernel.utils",
            },
        )
        self.assertIn("register_mcp_routes(", source)
        self.assertNotIn('@http.get("/mcp/tools")', source)
        self.assertNotIn('@http.post("/mcp/call")', source)
        self.assertNotIn("tool name is required", source)
        self.assertNotIn("arguments must be an object", source)
        self.assertNotIn("context must be an object", source)
        self.assertIn('"/mcp/tools"', mcp_source)
        self.assertIn('"/mcp/call"', mcp_source)
        self.assertIn("tool name is required", mcp_source)
        self.assertIn("arguments must be an object", mcp_source)
        self.assertIn("context must be an object", mcp_source)

    def test_control_data_plane_http_routes_are_lifted_out_of_main_factory(
        self,
    ) -> None:
        source = _api_app_source()
        data_plane_source = (
            SURFACE_ROOT / "transport" / "data_plane_http.py"
        ).read_text(encoding="utf-8")

        imports = _import_module_names(SURFACE_ROOT / "transport" / "data_plane_http.py")
        self.assertIn("artifacts.facade", imports)
        self.assertIn("feed.facade", imports)
        self.assertIn("sandbox.facade", imports)
        self.assertNotIn("feed.feed", imports)
        self.assertIn("register_data_plane_routes(", source)
        self.assertIn("authorize_data_plane_project", source)
        self.assertNotIn("task_queue=", source)
        self.assertNotIn("def _required_text", source)
        self.assertNotIn("def _decode_b64_field", source)
        self.assertNotIn("base64", source)
        for route in (
            '"/api/data-plane/resources/validate-association"',
            '"/api/data-plane/resources/observe"',
            '"/api/data-plane/resources/associate"',
            '"/api/data-plane/feed/validate-post"',
            '"/api/data-plane/feed/post"',
            '"/api/data-plane/sandboxes/request"',
            '"/api/data-plane/sandboxes/attach"',
        ):
            with self.subTest(route=route):
                self.assertIn(route, data_plane_source)
                self.assertNotIn(route, source)

    def test_transport_delegates_resource_content_to_application_query(self) -> None:
        routes = (HTTP_API_PACKAGE / "resources.py").read_text(encoding="utf-8")
        views = _api_views_source()
        self.assertIn("content_query(", routes)
        self.assertIn("artifacts.submitted_figure", views)
        self.assertNotIn("ResourceService", routes)
        self.assertNotIn("pinned_text_for_version", routes)
        self.assertNotIn(
            "SELECT project_id, content_sha256 FROM resource_versions",
            routes + views,
        )
        self.assertNotIn("FROM report_figures", routes + views)
        self.assertNotIn(".blobs.get", routes + views)

    def test_transport_delegates_reflection_views_to_application_query(self) -> None:
        source = (HTTP_API_PACKAGE / "reflections.py").read_text(encoding="utf-8")
        for delegate in ("graphs.reflections", "graphs.project", "graphs.reflection_graph"):
            self.assertIn(delegate, source)
        for internal in ("reflection_waves", ".store", "reflection_signal", "open_reflection"):
            self.assertNotIn(internal, source)

    def test_project_member_routes_delegate_policy_to_project_service(self) -> None:
        source = (HTTP_API_PACKAGE / "projects.py").read_text(encoding="utf-8")

        self.assertIn("projects.members", source)
        self.assertIn("projects.add_member", source)
        self.assertIn("projects.remove_member", source)
        self.assertNotIn(".store", source)

    def test_sandbox_attachment_validation_is_research_owned(self) -> None:
        record_core = (SURFACE_ROOT / "control" / "record_core.py").read_text(
            encoding="utf-8"
        )
        control_app = (SURFACE_ROOT / "control" / "control_app.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("build_experiment_attachment_check", record_core)
        self.assertNotIn("SELECT project_id FROM experiments", record_core)
        self.assertIn(
            "attachment_check=self.research_core.assert_experiment_in_project",
            control_app,
        )

    def test_tenant_counter_query_keeps_sandbox_sql_out_of_kernel_and_surface(self) -> None:
        store = (BACKEND_ROOT / "kernel" / "state" / "store.py").read_text(
            encoding="utf-8"
        )
        quotas = (BACKEND_ROOT / "sandbox" / "quotas.py").read_text(encoding="utf-8")
        queries = (BACKEND_ROOT / "application" / "queries.py").read_text(
            encoding="utf-8"
        )

        start = store.index("    def tenant_event_count(")
        end = store.index("\n    def ", start + 5)
        self.assertNotIn("sandbox_generations", store[start:end])
        self.assertIn("def tenant_generation_counters", quotas)
        self.assertIn("class TenantCountersQuery", queries)

    def test_surface_raw_control_app_access_baseline_only_shrinks(self) -> None:
        current = _raw_control_app_accesses()
        self.assertEqual(current, RAW_CONTROL_APP_ACCESS_BASELINE)

    def test_whole_control_app_carrier_baseline_only_shrinks(self) -> None:
        current = _whole_control_app_carriers()
        self.assertEqual(current, WHOLE_CONTROL_APP_CARRIER_BASELINE)

    def test_http_transport_does_not_own_raw_persistence(self) -> None:
        def enclosing_function(
            node: ast.AST, parents: dict[ast.AST, ast.AST]
        ) -> str | None:
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, ast.FunctionDef):
                    return parent.name
                parent = parents.get(parent)
            return None

        def stringish(node: ast.AST) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                parts: list[str] = []
                for value in node.values:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        parts.append(value.value)
                    else:
                        parts.append("{}")
                return "".join(parts)
            return None

        sql_re = re.compile(
            r"(?is)^\s*(WITH\b|PRAGMA\b|CREATE\s+TABLE\b|ALTER\s+TABLE\b|DROP\s+TABLE\b|SELECT\b|INSERT\b.+\bINTO\b|UPDATE\b.+\bSET\b|DELETE\b.+\bFROM\b)"
        )

        for path in HTTP_TRANSPORT_MODULES:
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                parents: dict[ast.AST, ast.AST] = {}
                for parent in ast.walk(tree):
                    for child in ast.iter_child_nodes(parent):
                        parents[child] = parent

                raw_sql: list[tuple[int, str]] = []
                execute_calls: list[int] = []
                connect_calls: list[tuple[str, str, int]] = []
                for node in ast.walk(tree):
                    text = stringish(node)
                    if text is not None and sql_re.search(text):
                        raw_sql.append((node.lineno, text.strip().splitlines()[0]))
                    if isinstance(node, ast.Call) and isinstance(
                        node.func, ast.Attribute
                    ):
                        if node.func.attr == "execute":
                            execute_calls.append(node.lineno)
                        if node.func.attr == "connect":
                            connect_calls.append(
                                (
                                    enclosing_function(node, parents) or "<module>",
                                    ast.unparse(node.func.value),
                                    node.lineno,
                                )
                            )

                self.assertEqual(raw_sql, [])
                self.assertEqual(execute_calls, [])
                self.assertEqual(connect_calls, [])

    def test_transport_delegates_graph_resolution_to_application_query(self) -> None:
        source = _api_package_source()
        self.assertIn("graphs.experiment", source)
        self.assertIn("graphs.reflection_graph", source)
        self.assertNotIn(".graph_refs", source)
        self.assertNotIn("_resolve_graph_refs", source)
        self.assertNotIn("_resolve_one_graph_ref", source)
        self.assertNotIn("_graph_ref_resource", source)
        self.assertNotIn("FROM claims WHERE id = ?", source)
        self.assertNotIn("FROM experiments WHERE id = ?", source)
        self.assertNotIn("FROM reviews", source)
        self.assertNotIn("FROM reflections", source)

    def test_graph_ref_resolver_uses_reference_type_registry(self) -> None:
        source = (RESEARCH_CORE / "graph_refs.py").read_text(encoding="utf-8")
        application = (BACKEND_ROOT / "application/queries.py").read_text(
            encoding="utf-8"
        )
        artifacts = (ARTIFACTS_ROOT / "facade.py").read_text(encoding="utf-8")
        evidence = (ARTIFACTS_ROOT / "ports" / "evidence.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("class GraphRefType:", source)
        self.assertIn("GRAPH_REF_TYPES: tuple[GraphRefType, ...]", source)
        self.assertEqual(source.count("GraphRefType("), 6)
        self.assertIn("for ref_type in GRAPH_REF_TYPES:", source)
        self.assertNotIn("EvidenceReader", source)
        self.assertNotIn("resolve_resource_reference", source)
        self.assertIn("def _refs_from_graph(", application)
        self.assertIn("self.artifacts.resolve_resource_reference", application)
        self.assertIn('elif ref.startswith("res_")', application)
        self.assertIn("def resolve_resource_reference(", artifacts)
        self.assertNotIn("resolve_resource_reference", evidence)
        for prefix in ("rev_", "claim_", "exp_", "syn_", "lit_", "paper_"):
            self.assertIn(f'prefix="{prefix}"', source)
            self.assertNotIn(f'if ref.startswith("{prefix}")', source)
            self.assertNotIn(f'elif ref.startswith("{prefix}")', source)

    def test_transport_has_no_visible_project_lookup_gate(self) -> None:
        source = _api_package_source()
        self.assertNotIn("project_ids_for_tenant", source)
        self.assertNotIn("SELECT id FROM projects WHERE tenant_id", source)

    def test_permission_service_owns_only_tool_authorization(self) -> None:
        permission_path = SERVICES / "permissions.py"
        self.assertEqual(
            _class_method_names(permission_path, "PermissionService"),
            {"reject_reviewer_mutation"},
        )
        permission_imports = _import_module_names(permission_path)
        self.assertNotIn("merv.shared.artifact_roles", permission_imports)
        self.assertFalse(
            any("research_core" in module for module in permission_imports)
        )
        for path in sorted(BACKEND_ROOT.rglob("*.py")):
            with self.subTest(module=str(path.relative_to(BACKEND_ROOT))):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom) or not node.module:
                        continue
                    if node.module.split(".")[-1] != "permissions":
                        continue
                    leaked = VOCABULARY_NAMES & {alias.name for alias in node.names}
                    self.assertFalse(
                        leaked,
                        f"import vocabulary from domain.vocabulary/artifacts.roles, not permissions: {sorted(leaked)}",
                    )

    def test_review_verdict_contract_uses_domain_vocabulary(self) -> None:
        from merv.brain.surface.tools.contracts import ReviewSubmitInput
        from merv.brain.research_core.domain.vocabulary import (
            REVIEW_VERDICT_VALUES,
            REVIEW_VERDICTS,
        )

        self.assertEqual(REVIEW_VERDICTS, frozenset(REVIEW_VERDICT_VALUES))
        self.assertEqual(
            set(ReviewSubmitInput.model_fields["verdict"].annotation.__args__),
            set(REVIEW_VERDICT_VALUES),
        )
        source = (SURFACE_ROOT / "tools" / "contracts.py").read_text(encoding="utf-8")
        self.assertIn("REVIEW_VERDICT_VALUES", source)
        self.assertIn("verdict: Literal[*REVIEW_VERDICT_VALUES]", source)
        self.assertNotIn('verdict: Literal["pass", "needs_changes", "fail"]', source)

    def test_gate_tables_are_domain_policy_only(self) -> None:
        # Workflow state machines are domain policy: they may share neutral
        # gate dataclasses and pure vocabulary (status vocabulary from domain,
        # role vocabulary from its artifacts owner), but never services.
        expected = {
            "workflow_gates.py": {"gates", "vocabulary", "typing"},
            "reflection_gates.py": {"gates", "merv.shared.artifact_roles", "typing"},
        }
        for name, imports in expected.items():
            with self.subTest(module=name):
                self.assertEqual(
                    _import_module_names(RESEARCH_CORE_DOMAIN / name), imports
                )

    def test_reflection_service_uses_experiment_name_leaf(self) -> None:
        self.assertNotIn(
            "experiments", _import_segments(RESEARCH_CORE / "reflections.py")
        )
        source = _rc_source("reflections.py")
        self.assertIn("experiment_writer: ReflectionExperimentWriter", source)
        self.assertNotIn("INSERT INTO experiments", source)
        self.assertNotIn("experiment_claims", source)

    def test_reflection_service_uses_claim_vocabulary(self) -> None:
        self.assertNotIn("claims", _import_segments(RESEARCH_CORE / "reflections.py"))
        source = _rc_source("reflections.py")
        self.assertIn(
            "reflection_writers", _import_segments(RESEARCH_CORE / "reflections.py")
        )
        self.assertIn("claims: ReflectionClaimWriter", source)
        self.assertNotIn("INSERT INTO claims", source)
        self.assertNotIn("UPDATE claims", source)

    def test_reflection_service_does_not_write_projects(self) -> None:
        source = _rc_source("reflections.py")
        self.assertNotIn("UPDATE projects", source)
        self.assertNotIn("project.stopped", source)

    def test_status_views_use_domain_vocabulary(self) -> None:
        for name in ("reflections.py",):
            with self.subTest(module=name):
                self.assertNotIn(
                    "workflow_gates", _import_segments(RESEARCH_CORE / name)
                )

    def test_identity_constants_are_foundation_vocabulary(self) -> None:
        from merv.brain.kernel.identity import LOCAL_CLIENT_ID, LOCAL_TENANT_ID
        from merv.brain.surface.identity import LOCAL_PRINCIPAL

        self.assertEqual(LOCAL_TENANT_ID, "local")
        self.assertEqual(LOCAL_CLIENT_ID, "local")
        self.assertEqual(LOCAL_PRINCIPAL.tenant_id, LOCAL_TENANT_ID)
        self.assertEqual(LOCAL_PRINCIPAL.client_id, LOCAL_CLIENT_ID)
        self.assertIn("kernel.identity", _import_module_names(SERVICES / "identity.py"))
        self.assertIn("kernel.identity", _import_module_names(RESEARCH_CORE / "reviews.py"))
        self.assertNotIn("services.identity", _rc_source("reviews.py"))

    def test_opaque_secret_token_helpers_are_single_sourced(self) -> None:
        self.assertEqual(
            _import_module_names(BACKEND_ROOT / "kernel" / "secret_tokens.py"),
            {"hashlib", "hmac", "secrets"},
        )
        sensitive_paths = (
            RESEARCH_CORE / "reviews.py",
            BACKEND_ROOT / "kernel" / "state" / "store.py",
        )
        for path in sensitive_paths:
            with self.subTest(module=path.relative_to(BACKEND_ROOT).as_posix()):
                modules = _import_module_names(path)
                self.assertNotIn("hashlib", modules)
                self.assertNotIn("secrets", modules)
                # kernel-internal imports say "secret_tokens"; research_core
                # routes through the kernel package ("kernel.secret_tokens").
                self.assertTrue(
                    any(
                        module == "secret_tokens" or module.endswith(".secret_tokens")
                        for module in modules
                    ),
                    f"{path.name} must source tokens from kernel/secret_tokens.py",
                )

        for path in (RESEARCH_CORE / "reviews.py",):
            with self.subTest(module=path.relative_to(BACKEND_ROOT).as_posix()):
                self.assertNotIn("hmac", _import_module_names(path))
                self.assertNotIn("compare_digest(", path.read_text(encoding="utf-8"))

        self.assertNotIn(
            "def _hash_capability",
            (RESEARCH_CORE / "reviews.py").read_text(encoding="utf-8"),
        )

    def test_experiment_projection_is_application_owned_and_pure(self) -> None:
        projection = BACKEND_ROOT / "application" / "experiments" / "presentation.py"
        modules = {
            module.split(".", 1)[0] for module in _import_module_names(projection)
        }

        self.assertFalse((RESEARCH_CORE / "experiment_views.py").exists())
        self.assertEqual(
            modules, {"claim_guidance", "gate_checklist", "ports", "research_core", "typing"}
        )
        self.assertNotIn("experiments", modules)
        self.assertNotIn("workflow", modules)


if __name__ == "__main__":
    unittest.main()
