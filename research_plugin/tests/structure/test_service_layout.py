from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tests.paths import BACKEND_ROOT, DOMAIN_ROOT, PLUGIN_ROOT, SERVICES_ROOT

ROOT = PLUGIN_ROOT
SERVICES = SERVICES_ROOT


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


VOCABULARY_NAMES = {
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
        self.assertNotIn("def report_problems", source)
        self.assertNotIn("def plan_sections_missing", source)
        self.assertNotIn("REQUIRED_PLAN_SECTIONS", source)
        self.assertNotIn("_HEADING_RE", source)

    def test_record_services_do_not_create_local_workspaces(self) -> None:
        for name in ("experiments.py", "syntheses.py"):
            with self.subTest(module=name):
                source = _source(name)
                self.assertNotIn("ensure_workspace", source)
                self.assertNotIn("_ensure_workspace", source)
                self.assertFalse(
                    _import_modules(name) & LOCAL_FS_IMPORTS,
                    f"{name} should not import local filesystem helpers",
                )
                self.assertNotIn(".mkdir(", source)
                self.assertNotIn("open(", source)

    def test_workflow_service_keeps_agent_projection_out(self) -> None:
        source = _source("workflow.py")

        self.assertNotIn("def slim_status_and_next", source)
        self.assertNotIn("def _slim_experiment", source)
        self.assertNotIn("def _sandbox_summary", source)
        self.assertNotIn("TERMINAL_EXPERIMENT_STATUSES", source)
        self.assertNotIn("ACTIVE_PROCESS_STATUSES =", source)

    def test_artifact_lint_is_a_leaf_module(self) -> None:
        # Pure text lint: regexes, a callback type, and shared domain markdown
        # image parsing. No filesystem imports — figure resolution is the
        # caller's business (submission capture).
        self.assertEqual(_import_modules("artifacts.py"), {"re", "collections", "domain"})

    def test_metrics_archive_service_module_is_a_port(self) -> None:
        # The concrete file/HTTP/SQLite archive lives in dataplane. Services
        # depend on this narrow port so they do not pull local capture code
        # into the control-safe service package.
        self.assertEqual(_import_modules("metrics_archive.py"), {"pathlib", "typing"})
        source = _source("metrics_archive.py")
        for forbidden in ("httpx", "sqlite3", "json", "tempfile", "os."):
            self.assertNotIn(forbidden, source)

    def test_sandbox_lifecycle_module_is_a_port(self) -> None:
        self.assertEqual(_import_modules("sandbox_lifecycle.py"), {"datetime", "typing"})

    def test_sandbox_lifecycle_workers_use_ports_not_concrete_services(self) -> None:
        self.assertNotIn(
            "experiments", _import_segments(SERVICES / "sandbox_provisioner.py")
        )
        daemon_imports = _import_segments(SERVICES / "sandbox_daemons.py")
        self.assertNotIn("experiments", daemon_imports)
        self.assertNotIn("sandbox_provisioner", daemon_imports)

    def test_resource_registration_observation_uses_observer_port(self) -> None:
        source = _source("resources.py")
        start = source.index("    def _register_one(")
        end = source.index("    def record_observation(")
        register_slice = source[start:end]

        self.assertIn("self.observer.observe_file", register_slice)
        self.assertNotIn("_resolve_repo_file", register_slice)
        self.assertNotIn("_content_sha256", register_slice)
        self.assertNotIn(".stat(", register_slice)

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
        self.assertNotIn("permissions", _import_segments(SERVICES / "resources.py"))

    def test_review_service_uses_permission_port(self) -> None:
        self.assertNotIn("permissions", _import_segments(SERVICES / "reviews.py"))

    def test_review_service_uses_target_ports(self) -> None:
        imports = _import_segments(SERVICES / "reviews.py")

        self.assertNotIn("experiments", imports)
        self.assertNotIn("syntheses", imports)

    def test_feed_service_does_not_read_local_image_paths(self) -> None:
        source = _source("feed.py")

        self.assertNotIn("resolve_repo_relative_file", source)
        self.assertNotIn(".read_bytes(", source)
        self.assertNotIn("workspace", source)
        self.assertIn("post_observed", source)

    def test_graph_lint_is_a_leaf_module(self) -> None:
        self.assertEqual(_import_modules("graph_lint.py"), {"json"})

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

    def test_synthesis_gates_only_reuses_workflow_gate_dataclasses(self) -> None:
        # The second gate table must not grow service dependencies: it shares
        # the experiment table's dataclasses and pure domain role names only.
        self.assertEqual(
            _import_module_names(SERVICES / "synthesis_gates.py"),
            {"domain.vocabulary", "typing", "workflow_gates"},
        )

    def test_synthesis_service_uses_experiment_name_leaf(self) -> None:
        self.assertNotIn("experiments", _import_segments(SERVICES / "syntheses.py"))

    def test_synthesis_service_uses_claim_vocabulary(self) -> None:
        self.assertNotIn("claims", _import_segments(SERVICES / "syntheses.py"))

    def test_view_modules_do_not_import_service_state_machines(self) -> None:
        for name in ("experiment_views.py", "workflow_views.py"):
            with self.subTest(module=name):
                modules = _import_modules(name)
                self.assertNotIn("experiments", modules)
                self.assertNotIn("workflow", modules)


if __name__ == "__main__":
    unittest.main()
