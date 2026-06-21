from __future__ import annotations

import ast
import re
import unittest
from inspect import Parameter, signature as inspect_signature
from pathlib import Path
from typing import Any, Protocol, get_type_hints, is_typeddict

from tests.paths import BACKEND_ROOT, DOMAIN_ROOT, PLUGIN_ROOT, PORTS_ROOT, SERVICES_ROOT

ROOT = PLUGIN_ROOT
SERVICES = SERVICES_ROOT
HTTP_TRANSPORT_MODULES = (
    BACKEND_ROOT / "transport" / "admin_http.py",
    BACKEND_ROOT / "transport" / "daemon_http.py",
    BACKEND_ROOT / "daemon_loopback.py",
    BACKEND_ROOT / "transport" / "feed_http.py",
    BACKEND_ROOT / "transport" / "http_api.py",
    BACKEND_ROOT / "transport" / "mcp_http.py",
)


def _source(name: str) -> str:
    return (SERVICES / name).read_text(encoding="utf-8")


def _import_modules(name: str) -> set[str]:
    return {module.split(".", 1)[0] for module in _import_module_names(SERVICES / name)}


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
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef)
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
            raise AssertionError(
                f"must not dynamically access self.{collaborator}"
            )
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
    "LOCAL_CLIENT_ID",
    "LOCAL_TENANT_ID",
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
        source = _source("experiments.py")

        self.assertNotIn("def slim_experiment_state", source)
        self.assertNotIn("experiment_views", _import_segments(SERVICES / "experiments.py"))
        self.assertNotIn("def get_state_agent", source)
        self.assertNotIn("def list_experiments_agent", source)
        self.assertNotIn("def report_problems", source)
        self.assertNotIn("def plan_sections_missing", source)
        self.assertNotIn("REQUIRED_PLAN_SECTIONS", source)
        self.assertNotIn("_HEADING_RE", source)

    def test_record_services_do_not_create_local_workspaces(self) -> None:
        for name in ("experiments.py", "syntheses.py"):
            with self.subTest(module=name):
                source = _source(name)
                import_modules = _import_module_names(SERVICES / name)
                self.assertNotIn("ensure_workspace", source)
                self.assertNotIn("_ensure_workspace", source)
                self.assertNotIn("reflection_policy", import_modules)
                self.assertFalse(
                    _import_modules(name) & LOCAL_FS_IMPORTS,
                    f"{name} should not import local filesystem helpers",
                )
                self.assertNotIn(".mkdir(", source)
                self.assertNotIn("open(", source)

    def test_workflow_service_keeps_agent_projection_out(self) -> None:
        source = _source("workflow.py")
        imports = _import_segments(SERVICES / "workflow.py")

        self.assertNotIn("def slim_status_and_next", source)
        self.assertNotIn("def _slim_experiment", source)
        self.assertNotIn("def _sandbox_summary", source)
        self.assertNotIn("TERMINAL_EXPERIMENT_STATUSES", source)
        self.assertNotIn("ACTIVE_PROCESS_STATUSES =", source)
        self.assertNotIn("resources", imports)
        self.assertFalse(
            {"experiments", "reviews", "sandboxes", "syntheses"} & imports
        )
        self.assertIn("workflow_readers", imports)
        for protocol_name in (
            "class ExperimentWorkflowReader",
            "class ReviewWorkflowReader",
            "class SandboxWorkflowReader",
            "class ReflectionWorkflowReader",
        ):
            self.assertNotIn(protocol_name, source)
        self.assertIn("experiments: ExperimentWorkflowReader", source)
        workflow_reader_path = PORTS_ROOT / "workflow_readers.py"
        for collaborator, protocol_name in (
            ("experiments", "ExperimentWorkflowReader"),
            ("reviews", "ReviewWorkflowReader"),
            ("sandboxes", "SandboxWorkflowReader"),
            ("syntheses", "ReflectionWorkflowReader"),
        ):
            allowed_calls = _class_method_names(workflow_reader_path, protocol_name)
            self.assertEqual(
                _strict_self_collaborator_call_names(
                    source, collaborator, allowed_calls
                ),
                allowed_calls,
            )

        from backend.services.workflow import WorkflowService

        get_type_hints(WorkflowService.__init__)

    def test_artifact_lint_is_domain_leaf_module(self) -> None:
        # Pure text lint: regexes, a callback type, and shared domain markdown
        # image parsing. No filesystem imports — figure resolution is the
        # caller's business (submission capture).
        self.assertEqual(
            _import_module_names(DOMAIN_ROOT / "artifacts.py"),
            {"re", "collections.abc", "markdown_images"},
        )

    def test_resource_selection_is_domain_leaf_module(self) -> None:
        self.assertEqual(
            _import_module_names(DOMAIN_ROOT / "resource_selection.py"),
            {"typing"},
        )

    def test_http_policy_is_fastapi_free(self) -> None:
        imports = _import_module_names(BACKEND_ROOT / "transport" / "http_policy.py")

        self.assertEqual(imports, {"dataclasses"})

    def test_ports_are_neutral_and_outside_services(self) -> None:
        expected_imports = {
            "metrics_archive.py": {"pathlib", "typing"},
            "mgmt_keys.py": {"pathlib", "typing"},
            "quota_admission.py": {"domain.quota_contract", "typing"},
            "review_policy.py": {"typing"},
            "resource_records.py": {"typing"},
            "sandbox_lifecycle.py": {"datetime", "typing"},
            "sandbox_sync.py": {"typing"},
            "sandbox_worker.py": {"pathlib", "typing"},
            "synthesis_writers.py": {"typing"},
            "task_channel.py": {"typing"},
            "workflow_readers.py": {"typing"},
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
        self.assertIn(
            "def sync_targets(self, *, tenant_id: str | None = None)",
            (PORTS_ROOT / "sandbox_sync.py").read_text(encoding="utf-8"),
        )
        sandbox_sync_path = PORTS_ROOT / "sandbox_sync.py"
        sandbox_sync_source = sandbox_sync_path.read_text(encoding="utf-8")
        self.assertIn("class RunningSandboxSyncRow", sandbox_sync_source)
        self.assertIn("class SyncTarget", sandbox_sync_source)
        self.assertIn("class RunningSandboxRows", sandbox_sync_source)
        self.assertEqual(
            _class_method_names(sandbox_sync_path, "ControlPlaneView"),
            {"sync_targets"},
        )
        self.assertEqual(
            _class_method_names(sandbox_sync_path, "RunningSandboxRows"),
            {"list_running_sync_rows"},
        )
        self.assertEqual(
            _class_method_names(sandbox_sync_path, "SyncSessionIssuer"),
            {"grant"},
        )
        from backend.ports.sandbox_sync import (
            ControlPlaneView,
            RunningSandboxRows,
            RunningSandboxSyncRow,
            SyncSessionIssuer,
            SyncTarget,
        )

        for protocol in (ControlPlaneView, RunningSandboxRows, SyncSessionIssuer):
            self.assertIn(Protocol, protocol.__mro__)
        self.assertTrue(is_typeddict(RunningSandboxSyncRow))
        self.assertEqual(
            get_type_hints(RunningSandboxSyncRow),
            {
                "experiment_id": str,
                "tenant_id": str | None,
                "sandbox_id": str | None,
                "ssh_host": str | None,
                "ssh_port": int | None,
                "ssh_user": str | None,
                "sync_dir": str | None,
                "workdir": str | None,
                "sandbox_data_dir": str | None,
                "unsynced_dir": str | None,
            },
        )
        self.assertTrue(is_typeddict(SyncTarget))
        self.assertEqual(get_type_hints(SyncTarget)["row"], RunningSandboxSyncRow)
        self.assertEqual(get_type_hints(SyncTarget)["session"], dict[str, Any])
        self.assertEqual(
            get_type_hints(RunningSandboxRows.list_running_sync_rows)["return"],
            list[RunningSandboxSyncRow],
        )
        self.assertEqual(
            get_type_hints(ControlPlaneView.sync_targets)["return"],
            list[SyncTarget],
        )
        grant_params = inspect_signature(SyncSessionIssuer.grant).parameters
        self.assertEqual(
            list(grant_params),
            [
                "self",
                "experiment_id",
                "sandbox_id",
                "ssh_host",
                "ssh_port",
                "ssh_user",
                "experiment_dir",
                "data_dir",
            ],
        )
        for name in list(grant_params)[1:]:
            self.assertEqual(grant_params[name].kind, Parameter.KEYWORD_ONLY)
        self.assertEqual(grant_params["data_dir"].default, "")
        workflow_reader_source = (PORTS_ROOT / "workflow_readers.py").read_text(
            encoding="utf-8"
        )
        for class_name in (
            "class ExperimentWorkflowReader",
            "class ReviewWorkflowReader",
            "class SandboxWorkflowReader",
            "class ReflectionWorkflowReader",
        ):
            self.assertIn(class_name, workflow_reader_source)
        from backend.ports.workflow_readers import (
            ExperimentWorkflowReader,
            ReflectionWorkflowReader,
            ReviewWorkflowReader,
            SandboxWorkflowReader,
        )

        for reader in (
            ExperimentWorkflowReader,
            ReviewWorkflowReader,
            SandboxWorkflowReader,
            ReflectionWorkflowReader,
        ):
            self.assertIn(Protocol, reader.__mro__)
        resource_record_path = PORTS_ROOT / "resource_records.py"
        resource_record_source = resource_record_path.read_text(encoding="utf-8")
        self.assertIn("class ResourceObservation", resource_record_source)
        self.assertIn("class ResourceObserver", resource_record_source)
        self.assertIn("class ResourceAssociationPolicy", resource_record_source)
        self.assertEqual(
            _class_method_names(resource_record_path, "ResourceObserver"),
            {"observe_file"},
        )
        self.assertEqual(
            _class_method_names(resource_record_path, "ResourceAssociationPolicy"),
            {"validate_resource_association", "storage_resource_target_type"},
        )
        from backend.ports.resource_records import ResourceObservation, ResourceObserver

        self.assertTrue(is_typeddict(ResourceObservation))
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
        synthesis_writer_path = PORTS_ROOT / "synthesis_writers.py"
        self.assertEqual(
            _class_method_names(synthesis_writer_path, "SynthesisClaimWriter"),
            {"create_from_synthesis", "update_from_synthesis"},
        )
        self.assertEqual(
            _class_method_names(synthesis_writer_path, "SynthesisExperimentWriter"),
            {"create_from_synthesis"},
        )
        self.assertEqual(
            _class_method_names(synthesis_writer_path, "SynthesisProjectWriter"),
            {"stop_from_synthesis"},
        )
        from backend.ports.synthesis_writers import (
            SynthesisClaimWriter,
            SynthesisExperimentWriter,
            SynthesisProjectWriter,
        )

        self.assertIn(Protocol, SynthesisClaimWriter.__mro__)
        self.assertIn(Protocol, SynthesisExperimentWriter.__mro__)
        self.assertIn(Protocol, SynthesisProjectWriter.__mro__)

    def test_sandbox_lifecycle_workers_use_ports_not_concrete_services(self) -> None:
        self.assertNotIn(
            "experiments",
            _import_segments(SERVICES / "sandbox" / "sandbox_provisioner.py"),
        )
        self.assertNotIn(
            "sandbox_mgmt_keys",
            _import_segments(SERVICES / "sandbox" / "sandboxes.py"),
        )
        self.assertFalse((SERVICES / "sandbox_mgmt_keys.py").exists())
        self.assertNotIn("class QuotaAdmission", _source("sandbox/sandboxes.py"))
        self.assertNotIn(
            "class ControlPlaneView", _source("sandbox/sandbox_daemons.py")
        )
        self.assertNotIn(
            "class SyncSessionIssuer", _source("sandbox/sandbox_provisioner.py")
        )
        daemon_imports = _import_segments(SERVICES / "sandbox" / "sandbox_daemons.py")
        self.assertNotIn("experiments", daemon_imports)
        self.assertNotIn("sandbox_provisioner", daemon_imports)

    def test_sync_sessions_use_running_sandbox_row_port(self) -> None:
        source = _source("sync_sessions.py")
        imports = _import_segments(SERVICES / "sync_sessions.py")

        self.assertIn("sandbox_sync", imports)
        self.assertIn("registry: RunningSandboxRows", source)
        self.assertIn("list_running_sync_rows", source)
        self.assertNotIn("list_running_rows", source)
        self.assertNotIn("class RunningSandboxRows", source)

    def test_auto_sync_loops_share_target_step(self) -> None:
        helper = BACKEND_ROOT / "sandbox_autosync.py"
        daemon_mode = BACKEND_ROOT / "composition" / "daemon_mode.py"
        daemon_source = daemon_mode.read_text(encoding="utf-8")
        local_source = _source("sandbox/sandbox_daemons.py")

        self.assertEqual(_import_module_names(helper), {"collections.abc", "typing"})
        self.assertIn("run_auto_sync_target", daemon_source)
        self.assertIn("run_auto_sync_target", local_source)
        self.assertNotIn("Mirror SandboxDaemons._auto_sync_loop", daemon_source)
        self.assertIn(
            '"experiment_id": str(row.get("experiment_id") or "")',
            daemon_source,
        )
        self.assertNotIn('target.get("experiment_id")', daemon_source)

    def test_resource_service_records_observations_without_local_observer(self) -> None:
        source = _source("resources.py")
        imports = _import_segments(SERVICES / "resources.py")

        self.assertIn("resource_records", imports)
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
        source = _source("resources.py")
        start = source.index("    def associate(")
        end = source.index("    def associate_observed(")
        associate_slice = source[start:end]

        self.assertIn("self.associate_observed", associate_slice)
        self.assertNotIn("_resolve_repo_file", source)
        self.assertNotIn("_ensure_current_version_for_resource", source)
        self.assertNotIn("_capture_gated_blob", source)

    def test_resource_service_has_no_local_file_reads(self) -> None:
        source = _source("resources.py")

        self.assertNotIn(".read_bytes(", source)
        self.assertNotIn("repo_root", source)
        self.assertNotIn("self.workspace", source)
        self.assertNotIn("backfill_gated_blobs", source)

    def test_resource_service_uses_permission_port(self) -> None:
        source = _source("resources.py")
        imports = _import_segments(SERVICES / "resources.py")

        self.assertNotIn("permissions", imports)
        self.assertIn("resource_records", imports)
        self.assertIn("permissions: ResourceAssociationPolicy", source)
        self.assertNotIn("class ResourceAssociationPolicy", source)

        from backend.services.resources import ResourceService

        get_type_hints(ResourceService.__init__)

    def test_review_service_uses_permission_port(self) -> None:
        imports = _import_segments(SERVICES / "reviews.py")
        self.assertNotIn("permissions", imports)
        self.assertNotIn("identity", imports)
        self.assertIn("domain", imports)
        self.assertIn("review_policy", imports)
        source = _source("reviews.py")
        self.assertIn("permissions: ReviewPolicy", source)
        self.assertNotIn("class ReviewPolicy", source)
        from backend.ports.review_policy import ReviewPolicy

        self.assertIn(Protocol, ReviewPolicy.__mro__)

    def test_review_return_policy_is_domain_leaf_module(self) -> None:
        self.assertEqual(
            _import_module_names(DOMAIN_ROOT / "review_returns.py"),
            {"dataclasses"},
        )
        imports = _import_module_names(SERVICES / "reviews.py")
        source = _source("reviews.py")

        self.assertIn("domain.review_returns", imports)
        self.assertIn("resolve_review_return", source)
        self.assertNotIn("experiment-attempt-review rejections must set", source)
        self.assertNotIn("project-reflection-review rejections must set", source)
        self.assertNotIn("experiment-design-review rejections cannot return_to", source)

    def test_review_gate_policy_is_domain_leaf_module(self) -> None:
        self.assertEqual(_import_module_names(DOMAIN_ROOT / "review_gates.py"), set())
        imports = _import_module_names(SERVICES / "reviews.py")
        source = _source("reviews.py")

        self.assertIn("domain.review_gates", imports)
        self.assertIn("expected_review_gate_role", source)
        self.assertIn("is_review_gate_exempt", source)
        self.assertNotIn('"synthesis_review":', source)
        self.assertNotIn('"design_review":', source)
        self.assertNotIn('"experiment_review":', source)

    def test_review_service_uses_direct_concrete_targets(self) -> None:
        imports = _import_segments(SERVICES / "reviews.py")

        self.assertIn("experiments", imports)
        self.assertIn("syntheses", imports)
        self.assertNotIn("review_targets", imports)
        source = _source("reviews.py")
        self.assertIn("experiments: ExperimentService", source)
        self.assertIn("syntheses: SynthesisService", source)
        self.assertNotIn("class ExperimentReviewTarget", source)
        self.assertNotIn("class SynthesisReviewTarget", source)
        from backend.services.experiments import ExperimentService
        from backend.services.reviews import ReviewService
        from backend.services.syntheses import SynthesisService

        hints = get_type_hints(ReviewService.__init__)
        self.assertIs(hints["experiments"], ExperimentService)
        self.assertIs(hints["syntheses"], SynthesisService)
        experiment_calls = {
            "get_state",
            "send_back_to_planned",
            "send_back_to_running",
            "target_snapshot_id",
        }
        synthesis_calls = {
            "get_state",
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
                source, "syntheses", synthesis_calls
            ),
            synthesis_calls,
        )

    def test_feed_service_does_not_read_local_image_paths(self) -> None:
        source = _source("feed.py")
        imports = _import_module_names(SERVICES / "feed.py")

        self.assertNotIn("from . import feed_policy", source)
        self.assertNotIn("feed_policy", imports)
        self.assertNotIn("resolve_repo_relative_file", source)
        self.assertNotIn(".read_bytes(", source)
        self.assertNotIn("workspace", source)
        self.assertIn("post_observed", source)

    def test_feed_policy_is_domain_leaf_module(self) -> None:
        self.assertEqual(_import_module_names(DOMAIN_ROOT / "feed_policy.py"), set())

    def test_experiment_names_are_domain_policy(self) -> None:
        self.assertEqual(
            _import_module_names(DOMAIN_ROOT / "experiment_names.py"),
            {"re", "utils"},
        )
        for name in ("experiments.py", "syntheses.py"):
            with self.subTest(module=name):
                imports = _import_module_names(SERVICES / name)
                self.assertIn("domain.experiment_names", imports)
                self.assertNotIn("experiment_names", imports)

    def test_reflection_projection_is_domain_leaf_module(self) -> None:
        self.assertEqual(
            _import_module_names(DOMAIN_ROOT / "reflection_projection.py"),
            {"typing"},
        )
        for name in ("reflection_tools.py", "workflow_views.py"):
            with self.subTest(module=name):
                imports = _import_module_names(SERVICES / name)
                self.assertIn("domain.reflection_projection", imports)
                self.assertNotIn("reflection_projection", imports)
        reflection_imports = _import_segments(SERVICES / "reflection_tools.py")
        self.assertIn("syntheses", reflection_imports)
        self.assertNotIn("reflection_waves", reflection_imports)
        reflection_source = _source("reflection_tools.py")
        self.assertIn("syntheses: SynthesisService", reflection_source)
        self.assertNotIn("class ReflectionWaveStore", reflection_source)
        from backend.services.reflection_tools import ReflectionToolService
        from backend.services.syntheses import SynthesisService

        self.assertIs(
            get_type_hints(ReflectionToolService.__init__)["syntheses"],
            SynthesisService,
        )
        get_type_hints(ReflectionToolService.create)

    def test_graph_lint_is_domain_leaf_module(self) -> None:
        self.assertEqual(_import_module_names(DOMAIN_ROOT / "graph_lint.py"), {"json"})

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
        path = BACKEND_ROOT / "utils.py"
        self.assertEqual(_import_module_names(path), {"datetime", "uuid"})
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("resolve_repo_relative_file", source)
        self.assertNotIn("pathlib", source)
        self.assertNotIn("os.path", source)

        repo_paths = BACKEND_ROOT / "dataplane" / "repo_paths.py"
        self.assertEqual(_import_module_names(repo_paths), {"pathlib", "typing", "utils"})
        repo_path_source = repo_paths.read_text(encoding="utf-8")
        self.assertIn("def resolve_repo_path", repo_path_source)
        self.assertIn("def repo_relative_path", repo_path_source)

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
        self.assertEqual(_import_module_names(BACKEND_ROOT / "env.py"), {"collections.abc", "os"})
        for path in sorted(BACKEND_ROOT.rglob("*.py")):
            if path.name == "env.py":
                continue
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.relative_to(BACKEND_ROOT).as_posix()):
                self.assertNotIn("def env_flag", source)
                self.assertNotIn("def env_float", source)
                self.assertNotIn("RESEARCH_PLUGIN_ACTIVITY_STDERR\", \"\").lower()", source)
                self.assertNotIn("RESEARCH_PLUGIN_SANDBOX_REAPER\", \"1\").lower()", source)
                self.assertNotIn(
                    "RESEARCH_PLUGIN_SANDBOX_AUTO_RSYNC\", \"1\").lower()",
                    source,
                )

    def test_modal_integer_env_parsing_uses_shared_helper(self) -> None:
        source = (
            BACKEND_ROOT / "execution" / "backends" / "modal" / "config.py"
        ).read_text(encoding="utf-8")

        self.assertIn("from ....env import env_int", source)
        self.assertNotIn("def _env_int", source)
        self.assertNotIn("def _env_non_negative_int", source)
        self.assertNotIn("_positive_int(os.environ.get", source)
        self.assertIn("_modal_env_int(", source)
        self.assertIn("_positive_env_int(", source)
        self.assertIn("_non_negative_env_int(", source)

    def test_services_type_against_base_state_store(self) -> None:
        concrete_store_names = {"StateStore", "SqliteStateStore"}
        for path in sorted(SERVICES.rglob("*.py")):
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
                            not module or module[-1] in {"backend", "research_plugin"}
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
        source = (BACKEND_ROOT / "state" / "store.py").read_text(encoding="utf-8")
        base_source = source[
            source.index("class BaseStateStore:"):source.index("class StateStore(")
        ]
        self.assertIn("class Row(Protocol)", source)
        self.assertIn("class ResultCursor(Protocol)", source)
        self.assertIn("class Connection(Protocol)", source)
        self.assertIn("def connect(self) -> Connection:", base_source)
        self.assertIn(
            "def transaction(self) -> Iterator[Connection]:", base_source
        )
        self.assertIn("parameters: Sequence[Any] = ()", source)
        self.assertIn("def __enter__(self) -> Connection:", source)
        self.assertIn("tb: TracebackType | None", source)
        self.assertNotIn("sqlite3.", base_source)
        self.assertIn("def next_created_seq(*, conn: Connection", source)
        self.assertIn("row: Row | Mapping[str, Any] | None", source)

        from backend.state.store import Connection, ResultCursor, Row

        for protocol in (Row, ResultCursor, Connection):
            self.assertIn(Protocol, protocol.__mro__)

    def test_control_services_do_not_leak_sqlite_connection_types(self) -> None:
        for name in ("pinned.py", "resources.py", "sandbox/sandboxes.py"):
            with self.subTest(module=name):
                source = _source(name)
                self.assertNotIn("sqlite3.Connection", source)
                self.assertNotIn("sqlite3.Row", source)
                self.assertNotIn("import sqlite3", source)

    def test_transport_uses_contract_capabilities_for_sandbox_lifecycle_specials(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        contracts_source = (BACKEND_ROOT / "contracts.py").read_text(encoding="utf-8")

        self.assertNotIn('name == "sandbox.get"', source)
        self.assertNotIn('name != "sandbox.get"', source)
        self.assertNotIn('name == "sandbox.release"', source)
        self.assertIn("TOOL_CONTRACTS.get(name)", source)
        self.assertIn("contract.hosted_control_skip_final_pull", source)
        self.assertIn("contract.tenant_scoped_sandbox_lookup", source)
        self.assertIn("hosted_control_skip_final_pull=True", contracts_source)
        self.assertIn("tenant_scoped_sandbox_lookup=True", contracts_source)
        marker = "if (\n            surface.enforce_project_scope\n            and contract is not None\n            and contract.tenant_scoped_sandbox_lookup"
        start = source.index(marker)
        end = source.index("return result", start)
        block = source[start:end]
        self.assertIn("tenant_id=", block)
        self.assertIn("api.app.sandboxes.get", block)
        self.assertNotIn(".store.transaction", block)
        self.assertNotIn("require_project_id", block)

    def test_http_surface_policy_keeps_mode_decisions_named(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        policy_source = (BACKEND_ROOT / "transport" / "http_policy.py").read_text(encoding="utf-8")

        self.assertNotIn("class _HttpSurfacePolicy", source)
        self.assertIn("surface_policy: HttpSurfacePolicy | None = None", source)
        self.assertIn(
            "surface = surface_policy or HttpSurfacePolicy.for_surface(", source
        )
        control_source = (BACKEND_ROOT / "composition" / "control_mode.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("surface_policy=surface", control_source)
        for decision in (
            "CONTROL_REQUIRE_AUTH_ENV_VAR",
            "CONTROL_RESTRICT_CORS_ENV_VAR",
            "require_privileged_bearer_auth=True",
            "enforce_project_scope=True",
            "hosted_control=True",
            "expose_local_data_plane=False",
        ):
            with self.subTest(decision=decision):
                self.assertIn(decision, control_source)
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
            "require_bearer_auth",
            "require_privileged_bearer_auth",
            "restrict_cors",
            "hosted_control",
            "expose_local_data_plane",
            "accept_repo_root_context",
            "allow_data_plane_http",
            "allow_data_plane_tool_calls",
            "use_hosted_tool_policies",
            "enforce_project_scope",
            "release_uses_final_pull",
        ):
            with self.subTest(field_name=field_name):
                self.assertIn(field_name, policy_source)

    def test_http_transport_centralizes_project_scope_gate(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")

        self.assertIn("def require_project_scope(", source)
        self.assertNotIn(".store.require_project_id(", source)
        self.assertNotIn(".store.transaction(", source)
        self.assertIn("target.app.projects.require_project_scope(", source)
        self.assertGreaterEqual(source.count("require_project_scope("), 5)

    def test_hosted_tool_call_metadata_uses_policy_table(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        policy_source = (BACKEND_ROOT / "transport" / "http_policy.py").read_text(encoding="utf-8")
        from backend.transport.http_policy import HOSTED_CONTROL_TOOL_POLICIES

        self.assertEqual(
            set(HOSTED_CONTROL_TOOL_POLICIES),
            {"project.create", "project.list", "project.current", "review.start"},
        )
        self.assertIsNone(
            HOSTED_CONTROL_TOOL_POLICIES["project.create"].tenant_id_fallback
        )
        for tool_name in ("project.list", "project.current", "review.start"):
            self.assertEqual(
                HOSTED_CONTROL_TOOL_POLICIES[tool_name].tenant_id_fallback,
                "",
            )
        self.assertTrue(
            HOSTED_CONTROL_TOOL_POLICIES["review.start"].telemetry_from_review_request
        )
        self.assertNotIn("class _HostedToolPolicy", source)
        self.assertIn("HOSTED_CONTROL_TOOL_POLICIES", source)
        self.assertIn("HOSTED_CONTROL_TOOL_POLICIES", policy_source)
        for tool_name in (
            "project.create",
            "project.list",
            "project.current",
            "review.start",
        ):
            self.assertIn(f'"{tool_name}": HostedToolPolicy', policy_source)
            self.assertNotIn(f'if surface.hosted_control and name == "{tool_name}"', source)
        self.assertIn("telemetry_from_review_request=True", policy_source)
        self.assertIn("api.app.reviews.request_project_id(", source)
        self.assertNotIn("SELECT project_id FROM review_requests", source)

    def test_http_data_plane_capabilities_use_policy_table(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        policy_source = (BACKEND_ROOT / "transport" / "http_policy.py").read_text(encoding="utf-8")
        from backend.transport.http_policy import HTTP_DATA_PLANE_FEATURE_TO_TOOL

        self.assertEqual(
            HTTP_DATA_PLANE_FEATURE_TO_TOOL,
            {
                "resource_registration": "resource.register_file",
                "resource_association": "resource.associate",
                "sandbox_sync": "sandbox.sync",
            },
        )
        self.assertIn("HTTP_DATA_PLANE_FEATURE_TO_TOOL", policy_source)
        self.assertIn("HTTP_DATA_PLANE_FEATURE_TO_TOOL", source)
        self.assertIn("surface.data_plane_http_capabilities()", source)
        for feature, tool_name in HTTP_DATA_PLANE_FEATURE_TO_TOOL.items():
            with self.subTest(feature=feature):
                self.assertIn(f'feature="{feature}"', source)
                self.assertNotIn(f'tool="{tool_name}"', source)
                self.assertNotIn(
                    f'"{feature}": surface.allow_data_plane_http',
                    source,
                )

    def test_admin_http_routes_are_lifted_out_of_main_factory(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        admin_source = (BACKEND_ROOT / "transport" / "admin_http.py").read_text(encoding="utf-8")

        self.assertEqual(
            _import_module_names(BACKEND_ROOT / "transport" / "admin_http.py"),
            {"collections.abc", "typing", "fastapi", "observability"},
        )
        self.assertIn("register_admin_routes(", source)
        self.assertIn('"/api/admin/cleanup"', admin_source)
        self.assertIn('"/api/admin/tenants/{tenant_id}/counters"', admin_source)
        self.assertNotIn('"/api/admin/cleanup"', source)
        self.assertNotIn('"/api/admin/tenants/{tenant_id}/counters"', source)
        self.assertNotIn("TenantCounters", source)
        self.assertIn("store=api.app.store", source)
        self.assertNotIn("app=api.app", source)
        self.assertNotIn("app.", admin_source)
        self.assertIn("cleanup.run_all().as_dict()", admin_source)
        self.assertIn("require_admin(request)", admin_source)
        self.assertIn("require_tenant_or_admin(request, tenant_id)", admin_source)

    def test_mcp_http_routes_are_shared_by_control_and_daemon(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        daemon_source = (BACKEND_ROOT / "daemon_loopback.py").read_text(encoding="utf-8")
        mcp_source = (BACKEND_ROOT / "transport" / "mcp_http.py").read_text(encoding="utf-8")

        self.assertEqual(
            _import_module_names(BACKEND_ROOT / "transport" / "mcp_http.py"),
            {"collections.abc", "json", "typing", "fastapi", "utils"},
        )
        for owner_source in (source, daemon_source):
            self.assertIn("register_mcp_routes(", owner_source)
            self.assertNotIn('@http.get("/mcp/tools")', owner_source)
            self.assertNotIn('@http.post("/mcp/call")', owner_source)
            self.assertNotIn("tool name is required", owner_source)
            self.assertNotIn("arguments must be an object", owner_source)
            self.assertNotIn("context must be an object", owner_source)
        self.assertIn('"/mcp/tools"', mcp_source)
        self.assertIn('"/mcp/call"', mcp_source)
        self.assertIn("tool name is required", mcp_source)
        self.assertIn("arguments must be an object", mcp_source)
        self.assertIn("context must be an object", mcp_source)

    def test_control_daemon_http_routes_are_lifted_out_of_main_factory(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        daemon_source = (BACKEND_ROOT / "transport" / "daemon_http.py").read_text(encoding="utf-8")

        self.assertEqual(
            _import_module_names(BACKEND_ROOT / "transport" / "daemon_http.py"),
            {
                "base64",
                "binascii",
                "collections.abc",
                "typing",
                "fastapi",
                "services.feed",
                "utils",
            },
        )
        self.assertIn("register_daemon_routes(", source)
        self.assertIn("app_for_daemon_project", source)
        self.assertIn("task_queue=task_queue", source)
        self.assertIn("sync_targets_source=sync_targets_source", source)
        self.assertNotIn("def _required_text", source)
        self.assertNotIn("def _decode_b64_field", source)
        self.assertNotIn("base64", source)
        for route in (
            '"/api/daemon/tasks"',
            '"/api/daemon/tasks/{task_id}/ack"',
            '"/api/daemon/resources/validate-association"',
            '"/api/daemon/resources/observe"',
            '"/api/daemon/resources/associate"',
            '"/api/daemon/feed/validate-post"',
            '"/api/daemon/feed/post"',
            '"/api/daemon/sandboxes/request"',
            '"/api/daemon/sandboxes/sync"',
            '"/api/daemon/sandboxes/metrics"',
            '"/api/daemon/sync-targets"',
        ):
            with self.subTest(route=route):
                self.assertIn(route, daemon_source)
                self.assertNotIn(route, source)

    def test_transport_delegates_submitted_resource_blob_reads_to_service(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")

        self.assertIn("self.app.resources.pinned_text_for_version", source)
        self.assertIn("self.app.resources.submitted_text_for_version", source)
        self.assertIn("self.app.resources.submitted_figure", source)
        self.assertNotIn(
            "SELECT project_id, content_sha256 FROM resource_versions",
            source,
        )
        self.assertNotIn("FROM report_figures", source)
        self.assertNotIn("self.app.blobs.get", source)

    def test_transport_delegates_synthesis_overview_to_service(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        start = source.index("    def syntheses_view(")
        end = source.index("    def synthesis_detail(", start)
        block = source[start:end]

        self.assertIn("self.app.syntheses.overview", block)
        self.assertNotIn("self.app.store.connect", block)
        self.assertNotIn("reflection_signal", block)
        self.assertNotIn("open_synthesis", block)
        self.assertNotIn("latest_published", block)

        start = source.index("    def project_logic_graph(")
        end = source.index("    def synthesis_graph(", start)
        block = source[start:end]

        self.assertIn("self.app.syntheses.project_logic_graph_selection", block)
        self.assertNotIn("self.app.store.connect", block)
        self.assertNotIn("reflection_signal", block)
        self.assertNotIn("open_synthesis", block)
        self.assertNotIn("latest_published", block)
        self.assertNotIn("def _latest_graph_resource", source)

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

    def test_transport_delegates_graph_ref_resolution_to_service(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        self.assertIn("self.app.graph_refs.resolve_index", source)
        self.assertEqual(
            source.count('"ref_index": self.app.graph_refs.resolve_index('), 1
        )
        self.assertNotIn("_resolve_graph_refs", source)
        self.assertNotIn("_resolve_one_graph_ref", source)
        self.assertNotIn("_graph_ref_resource", source)
        self.assertNotIn("FROM claims WHERE id = ?", source)
        self.assertNotIn("FROM experiments WHERE id = ?", source)
        self.assertNotIn("FROM reviews", source)
        self.assertNotIn("FROM syntheses", source)

    def test_graph_ref_resolver_uses_reference_type_registry(self) -> None:
        source = (SERVICES / "graph_refs.py").read_text(encoding="utf-8")
        self.assertIn("class GraphRefType:", source)
        self.assertIn("GRAPH_REF_TYPES: tuple[GraphRefType, ...]", source)
        self.assertEqual(source.count("GraphRefType("), 5)
        self.assertIn("for ref_type in GRAPH_REF_TYPES:", source)
        for prefix in ("res_", "rev_", "claim_", "exp_", "syn_"):
            self.assertIn(f'prefix="{prefix}"', source)
            self.assertNotIn(f'if ref.startswith("{prefix}")', source)
            self.assertNotIn(f'elif ref.startswith("{prefix}")', source)

    def test_transport_delegates_visible_project_lookup_to_service(self) -> None:
        source = (BACKEND_ROOT / "transport" / "http_api.py").read_text(encoding="utf-8")
        self.assertIn("target.app.projects.project_ids_for_tenant", source)
        self.assertNotIn("SELECT id FROM projects WHERE tenant_id", source)

    def test_vocabulary_imports_bypass_permission_service(self) -> None:
        for path in sorted(BACKEND_ROOT.rglob("*.py")):
            if path == SERVICES / "permissions.py":
                continue
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
                        f"import vocabulary from backend.domain.vocabulary, not permissions: {sorted(leaked)}",
                    )

    def test_review_verdict_contract_uses_domain_vocabulary(self) -> None:
        from backend.contracts import ReviewSubmitInput
        from backend.domain.vocabulary import REVIEW_VERDICT_VALUES, REVIEW_VERDICTS

        self.assertEqual(REVIEW_VERDICTS, frozenset(REVIEW_VERDICT_VALUES))
        self.assertEqual(
            set(ReviewSubmitInput.model_fields["verdict"].annotation.__args__),
            set(REVIEW_VERDICT_VALUES),
        )
        source = (BACKEND_ROOT / "contracts.py").read_text(encoding="utf-8")
        self.assertIn("REVIEW_VERDICT_VALUES", source)
        self.assertIn("verdict: Literal[*REVIEW_VERDICT_VALUES]", source)
        self.assertNotIn('verdict: Literal["pass", "needs_changes", "fail"]', source)

    def test_gate_tables_are_domain_policy_only(self) -> None:
        # Workflow state machines are domain policy: they may share neutral
        # gate dataclasses and pure vocabulary, but must not depend on services.
        for name in ("workflow_gates.py", "synthesis_gates.py"):
            with self.subTest(module=name):
                self.assertEqual(
                    _import_module_names(DOMAIN_ROOT / name),
                    {"gates", "vocabulary", "typing"},
                )

    def test_synthesis_service_uses_experiment_name_leaf(self) -> None:
        self.assertNotIn("experiments", _import_segments(SERVICES / "syntheses.py"))
        source = _source("syntheses.py")
        self.assertIn("experiment_writer: SynthesisExperimentWriter", source)
        self.assertNotIn("INSERT INTO experiments", source)
        self.assertNotIn("experiment_claims", source)

    def test_synthesis_service_uses_claim_vocabulary(self) -> None:
        self.assertNotIn("claims", _import_segments(SERVICES / "syntheses.py"))
        source = _source("syntheses.py")
        self.assertIn("synthesis_writers", _import_segments(SERVICES / "syntheses.py"))
        self.assertIn("claims: SynthesisClaimWriter", source)
        self.assertNotIn("INSERT INTO claims", source)
        self.assertNotIn("UPDATE claims", source)

    def test_synthesis_service_uses_project_writer_for_hard_stop(self) -> None:
        source = _source("syntheses.py")
        self.assertIn("project_writer: SynthesisProjectWriter", source)
        self.assertNotIn("UPDATE projects", source)
        self.assertNotIn("project.stopped", source)

    def test_status_views_use_domain_vocabulary(self) -> None:
        for name in ("project_overview.py", "workflow_views.py", "syntheses.py"):
            with self.subTest(module=name):
                self.assertNotIn("workflow_gates", _import_segments(SERVICES / name))

    def test_identity_constants_are_domain_vocabulary(self) -> None:
        from backend.domain.vocabulary import LOCAL_CLIENT_ID, LOCAL_TENANT_ID
        from backend.services.identity import LOCAL_PRINCIPAL

        self.assertEqual(LOCAL_TENANT_ID, "local")
        self.assertEqual(LOCAL_CLIENT_ID, "local")
        self.assertEqual(LOCAL_PRINCIPAL.tenant_id, LOCAL_TENANT_ID)
        self.assertEqual(LOCAL_PRINCIPAL.client_id, LOCAL_CLIENT_ID)
        self.assertFalse(
            {"LOCAL_TENANT_ID", "LOCAL_CLIENT_ID"}
            & _assigned_names(SERVICES / "identity.py")
        )
        self.assertIn("domain.vocabulary", _import_module_names(SERVICES / "identity.py"))
        self.assertNotIn("identity", _import_segments(SERVICES / "reviews.py"))
        self.assertNotIn("services.identity", _source("reviews.py"))

    def test_opaque_secret_token_helpers_are_single_sourced(self) -> None:
        self.assertEqual(
            _import_module_names(BACKEND_ROOT / "secret_tokens.py"),
            {"hashlib", "hmac", "secrets"},
        )
        sensitive_paths = (
            BACKEND_ROOT / "composition" / "daemon_mode.py",
            BACKEND_ROOT / "services" / "identity.py",
            BACKEND_ROOT / "services" / "reviews.py",
            BACKEND_ROOT / "state" / "store.py",
        )
        for path in sensitive_paths:
            with self.subTest(module=path.relative_to(BACKEND_ROOT).as_posix()):
                self.assertNotIn("hashlib", _import_module_names(path))
                self.assertNotIn("secrets", _import_module_names(path))
                self.assertIn("secret_tokens", _import_module_names(path))

        for path in (
            BACKEND_ROOT / "services" / "identity.py",
            BACKEND_ROOT / "services" / "reviews.py",
        ):
            with self.subTest(module=path.relative_to(BACKEND_ROOT).as_posix()):
                self.assertNotIn("hmac", _import_module_names(path))
                self.assertNotIn("compare_digest(", path.read_text(encoding="utf-8"))

        self.assertNotIn(
            "def _hash_capability",
            (BACKEND_ROOT / "services" / "reviews.py").read_text(encoding="utf-8"),
        )

    def test_view_modules_do_not_import_service_state_machines(self) -> None:
        for name in ("experiment_views.py", "workflow_views.py"):
            with self.subTest(module=name):
                modules = _import_modules(name)
                self.assertNotIn("experiments", modules)
                self.assertNotIn("workflow", modules)

    def test_experiment_view_is_a_leaf_projection(self) -> None:
        self.assertEqual(_import_modules("experiment_views.py"), {"typing"})


if __name__ == "__main__":
    unittest.main()
