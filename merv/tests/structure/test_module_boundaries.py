"""Executable component and layer laws for the brain modular monolith.

``docs/MODULE_BOUNDARIES.md`` is the human-readable decision record.  Every
brain production file is independently classified by capability ownership
(component) and architectural role (layer).  Imports, including function-local
imports, must satisfy *both* laws.  Transitional layer violations are frozen as
exact file pairs: new violations fail and repaired pairs must be removed.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests.paths import BACKEND_ROOT

KERNEL = "kernel"
RESEARCH_CORE = "research_core"
ARTIFACTS = "artifacts"
OBJECT_STORAGE = "object_storage"
SANDBOX = "sandbox"
FEED = "feed"
MLFLOW = "mlflow"
APPLICATION_COMPONENT = "application"
SURFACE = "surface"

MODULES = (
    KERNEL,
    RESEARCH_CORE,
    ARTIFACTS,
    OBJECT_STORAGE,
    SANDBOX,
    FEED,
    MLFLOW,
    APPLICATION_COMPONENT,
    SURFACE,
)

# Directory-level component assignments (deepest matching prefix wins;
# FILE_COMPONENTS wins over all prefixes). Paths are brain-relative posix.
PACKAGE_COMPONENTS = {
    "kernel": KERNEL,
    "research_core": RESEARCH_CORE,
    "artifacts": ARTIFACTS,
    "object_storage": OBJECT_STORAGE,
    "sandbox": SANDBOX,
    "feed": FEED,
    "mlflow": MLFLOW,
    "application": APPLICATION_COMPONENT,
    "surface": SURFACE,
}

# File-level component overrides.
FILE_COMPONENTS = {
    # kernel: package root docstring/version shell.
    "__init__.py": KERNEL,
}

# Component answers "which capability owns this file?"  MLflow and concrete
# object storage are integrations, while cross-component coordination belongs
# to Application.  Surface is the outer delivery/composition component.
ALLOWED_COMPONENT_EDGES = (
    {(KERNEL, KERNEL)}
    | {(RESEARCH_CORE, dependency) for dependency in (RESEARCH_CORE, ARTIFACTS, KERNEL)}
    | {(ARTIFACTS, dependency) for dependency in (ARTIFACTS, KERNEL)}
    | {(SANDBOX, dependency) for dependency in (SANDBOX, KERNEL)}
    | {(FEED, dependency) for dependency in (FEED, KERNEL)}
    | {
        (APPLICATION_COMPONENT, dependency)
        for dependency in (
            APPLICATION_COMPONENT,
            RESEARCH_CORE,
            ARTIFACTS,
            SANDBOX,
            FEED,
            KERNEL,
        )
    }
    | {(MLFLOW, dependency) for dependency in (MLFLOW, APPLICATION_COMPONENT, KERNEL)}
    | {(OBJECT_STORAGE, dependency) for dependency in (OBJECT_STORAGE, KERNEL)}
    | {(SURFACE, dependency) for dependency in MODULES}
)

# Layer is independent of component ownership. A provider driver can therefore
# be an adapter in the Sandbox component, and StorageLedgerService can remain
# application policy in the Storage component until a later physical move.
FOUNDATION = "foundation"
PORT = "port"
DOMAIN = "domain"
APPLICATION_LAYER = "application"
ADAPTER = "adapter"
DELIVERY = "delivery"
BOOTSTRAP = "bootstrap"

LAYERS = (
    FOUNDATION,
    PORT,
    DOMAIN,
    APPLICATION_LAYER,
    ADAPTER,
    DELIVERY,
    BOOTSTRAP,
)

PACKAGE_LAYERS = {
    "kernel": FOUNDATION,
    "kernel/ports": PORT,
    "research_core": APPLICATION_LAYER,
    "research_core/domain": DOMAIN,
    "artifacts": APPLICATION_LAYER,
    "feed": APPLICATION_LAYER,
    "sandbox": APPLICATION_LAYER,
    "sandbox/execution/backends": ADAPTER,
    "mlflow": ADAPTER,
    "object_storage": ADAPTER,
    "application": APPLICATION_LAYER,
    "application/ports": PORT,
    "surface": DELIVERY,
    "surface/composition": BOOTSTRAP,
}

FILE_LAYERS = {
    "__init__.py": FOUNDATION,
    "kernel/state/dialects.py": ADAPTER,
    "artifacts/figure_view.py": DOMAIN,
    "artifacts/association_policy.py": DOMAIN,
    "artifacts/resource_selection.py": DOMAIN,
    "feed/feed_policy.py": DOMAIN,
    "feed/feed_unfurl.py": ADAPTER,
    "sandbox/sandbox_backend.py": PORT,
    "sandbox/execution/multiplexer.py": ADAPTER,
    "sandbox/execution/vm_ssh.py": ADAPTER,
    "sandbox/execution/__init__.py": BOOTSTRAP,
    "sandbox/execution/driver_registry.py": BOOTSTRAP,
    "sandbox/managed_mgmt_keys.py": ADAPTER,
    "sandbox/mgmt_keys.py": ADAPTER,
    "sandbox/ssh_keys.py": ADAPTER,
    "object_storage/service.py": APPLICATION_LAYER,
    "surface/config.py": BOOTSTRAP,
    "surface/transport/http_server.py": BOOTSTRAP,
    "surface/control/control_app.py": BOOTSTRAP,
    "surface/control/record_core.py": BOOTSTRAP,
    "surface/control/control_client.py": ADAPTER,
    "surface/control/control_runtime.py": ADAPTER,
}

ALLOWED_LAYER_EDGES = (
    {(FOUNDATION, FOUNDATION)}
    | {(PORT, dependency) for dependency in (PORT, FOUNDATION)}
    | {(DOMAIN, dependency) for dependency in (DOMAIN, PORT, FOUNDATION)}
    | {
        (APPLICATION_LAYER, dependency)
        for dependency in (APPLICATION_LAYER, DOMAIN, PORT, FOUNDATION)
    }
    | {
        (ADAPTER, dependency)
        for dependency in (ADAPTER, APPLICATION_LAYER, DOMAIN, PORT, FOUNDATION)
    }
    | {
        (DELIVERY, dependency)
        for dependency in (DELIVERY, APPLICATION_LAYER, PORT, FOUNDATION)
    }
    | {(BOOTSTRAP, dependency) for dependency in LAYERS}
)

# Exact-pair compatibility ledger for unrelated Surface work that has not yet
# moved inward. This may only shrink. Experiment-transition/exhibit pairs are
# deliberately absent from the final ledger.
LAYER_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # Feed still constructs its network adapter instead of receiving a
        # LinkUnfurlPort. This is the one named component-internal exception.
        ("feed/feed.py", "feed/feed_unfurl.py"),
        # Unrelated legacy Surface entrypoints. Move these dependencies inward
        # use case by use case; never broaden them to directory wildcards.
        ("surface/auth.py", "research_core/domain/vocabulary.py"),
        ("surface/identity.py", "research_core/domain/vocabulary.py"),
        ("surface/observability.py", "surface/config.py"),
        ("surface/tools/contracts.py", "research_core/domain/vocabulary.py"),
        ("surface/tools/contracts.py", "surface/config.py"),
    }
)


# SQL follows the import law (conformance scan, post-phase-6): a module's SQL
# may name its own tables, kernel tables, and tables of modules it is allowed
# to import. Tables absent here (projects, events, tenants, tool_calls,
# schema_migrations, *_migrate scratch) are kernel scoping infrastructure.
TABLE_OWNERS = {
    "experiments": RESEARCH_CORE,
    "experiment_claims": RESEARCH_CORE,
    "claims": RESEARCH_CORE,
    "reviews": RESEARCH_CORE,
    "review_requests": RESEARCH_CORE,
    "review_sessions": RESEARCH_CORE,
    "reflections": RESEARCH_CORE,
    "reflection_claim_changes": RESEARCH_CORE,
    "reflection_experiments": RESEARCH_CORE,
    "resources": ARTIFACTS,
    "resource_versions": ARTIFACTS,
    "resource_associations": ARTIFACTS,
    "report_figures": ARTIFACTS,
    "storage_objects": OBJECT_STORAGE,
    "sandboxes": SANDBOX,
    "sandbox_attachments": SANDBOX,
    "sandbox_generations": SANDBOX,
    "sandbox_runs": SANDBOX,
    "tenant_quotas": SANDBOX,
    "spend_kill_switches": SANDBOX,
    "posts": FEED,
    "feed_authors": FEED,
    "post_reactions": FEED,
}
SQL_TABLE_REF = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)\b", re.IGNORECASE)


def _backend_files() -> list[Path]:
    return sorted(
        path for path in BACKEND_ROOT.rglob("*.py") if "__pycache__" not in path.parts
    )


def _classify_from(
    rel: str,
    *,
    packages: dict[str, str],
    files: dict[str, str],
) -> str | None:
    if rel in files:
        return files[rel]
    parts = rel.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        prefix = "/".join(parts[:depth])
        if prefix in packages:
            return packages[prefix]
    return None


def _component(rel: str) -> str | None:
    return _classify_from(
        rel,
        packages=PACKAGE_COMPONENTS,
        files=FILE_COMPONENTS,
    )


def _layer(rel: str) -> str | None:
    return _classify_from(rel, packages=PACKAGE_LAYERS, files=FILE_LAYERS)


def _dotted_index() -> dict[str, str]:
    """Absolute dotted module name -> brain-relative file path."""
    index: dict[str, str] = {}
    for path in _backend_files():
        rel = path.relative_to(BACKEND_ROOT)
        parts = (
            rel.parent.parts
            if rel.name == "__init__.py"
            else (*rel.parent.parts, rel.stem)
        )
        index[".".join(("merv", "brain", *parts))] = rel.as_posix()
    return index


def _import_targets(path: Path, dotted: dict[str, str]) -> set[str]:
    """Brain files imported by ``path``, top-level and function-local alike.

    Relative imports resolve against the importing file's package; for
    ``from base import name`` the deeper ``base.name`` submodule wins when it
    exists, otherwise the edge points at ``base`` itself.
    """
    rel = path.relative_to(BACKEND_ROOT)
    package = ("merv", "brain", *rel.parent.parts)
    targets: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets.update(
                dotted[alias.name] for alias in node.names if alias.name in dotted
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = ".".join(package[: len(package) - (node.level - 1)])
                if node.module:
                    base = f"{base}.{node.module}"
            elif node.module and node.module != "__future__":
                base = node.module
            else:
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if candidate in dotted:
                    targets.add(dotted[candidate])
                elif base in dotted:
                    targets.add(dotted[base])
    return targets


def _import_pairs() -> set[tuple[str, str]]:
    dotted = _dotted_index()
    imports: set[tuple[str, str]] = set()
    for path in _backend_files():
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        for target in _import_targets(path, dotted):
            if target == rel:
                continue
            imports.add((rel, target))
    return imports


def _component_violations() -> set[tuple[str, str]]:
    return {
        (importer, target)
        for importer, target in _import_pairs()
        if (_component(importer), _component(target)) not in ALLOWED_COMPONENT_EDGES
    }


def _layer_violations() -> set[tuple[str, str]]:
    return {
        (importer, target)
        for importer, target in _import_pairs()
        if (_layer(importer), _layer(target)) not in ALLOWED_LAYER_EDGES
    }


class ModuleBoundaryTest(unittest.TestCase):
    def test_tool_handler_registry_is_delivery(self) -> None:
        self.assertEqual(_layer("surface/tools/tool_handlers.py"), DELIVERY)

    def test_every_backend_file_is_classified_by_component_and_layer(self) -> None:
        for label, classifier in (("component", _component), ("layer", _layer)):
            with self.subTest(classification=label):
                unclassified = sorted(
                    rel
                    for path in _backend_files()
                    if classifier(
                        rel := path.relative_to(BACKEND_ROOT).as_posix()
                    )
                    is None
                )
                self.assertFalse(
                    unclassified,
                    f"new brain files must be assigned a {label} in "
                    "tests/structure/test_module_boundaries.py: "
                    f"{unclassified}",
                )

    def test_classification_tables_carry_no_stale_paths(self) -> None:
        for table_name, paths in (
            ("FILE_COMPONENTS", FILE_COMPONENTS),
            ("FILE_LAYERS", FILE_LAYERS),
        ):
            for rel in sorted(paths):
                with self.subTest(table=table_name, file=rel):
                    self.assertTrue(
                        (BACKEND_ROOT / rel).is_file(),
                        f"stale {table_name} entry: {rel}",
                    )
        for table_name, paths in (
            ("PACKAGE_COMPONENTS", PACKAGE_COMPONENTS),
            ("PACKAGE_LAYERS", PACKAGE_LAYERS),
        ):
            for prefix in sorted(paths):
                with self.subTest(table=table_name, package=prefix):
                    self.assertTrue(
                        (BACKEND_ROOT / prefix).is_dir(),
                        f"stale {table_name} entry: {prefix}",
                    )

    def test_component_import_law(self) -> None:
        violations = sorted(_component_violations())
        self.assertFalse(
            violations,
            "component-boundary violation (see docs/MODULE_BOUNDARIES.md): "
            + ", ".join(
                f"{importer} -> {target} "
                f"[{_component(importer)} -> {_component(target)}]"
                for importer, target in violations
            ),
        )

    def test_no_new_layer_boundary_violations(self) -> None:
        new = sorted(_layer_violations() - LAYER_EXCEPTIONS)
        self.assertFalse(
            new,
            "new layer-boundary violation (see docs/MODULE_BOUNDARIES.md): "
            + ", ".join(
                f"{importer} -> {target} [{_layer(importer)} -> {_layer(target)}]"
                for importer, target in new
            ),
        )

    def test_module_sql_respects_table_ownership(self) -> None:
        """Fitness assertion (conformance scan, no grandfathering): SQL string
        literals follow the same edges as imports — a module may only name its
        own tables, kernel tables, and tables of modules it may import.
        Supersedes the phase-4a sandbox-only lint. Cross-module reads belong
        behind composition-injected callables (see control/record_core.py)."""
        offenders: list[str] = []
        for path in _backend_files():
            rel = path.relative_to(BACKEND_ROOT).as_posix()
            module = _component(rel)
            if module in (None, KERNEL, SURFACE):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                for match in SQL_TABLE_REF.finditer(node.value):
                    owner = TABLE_OWNERS.get(match.group(1).lower())
                    if owner is None or owner == module:
                        continue
                    if (module, owner) not in ALLOWED_COMPONENT_EDGES:
                        offenders.append(
                            f"{rel}:{node.lineno} ({module} SQL names "
                            f"{owner} table {match.group(1)})"
                        )
        self.assertFalse(
            offenders,
            "module SQL crosses an unratified boundary; inject the query from "
            "the owning module at composition instead: "
            + ", ".join(sorted(set(offenders))),
        )

    def test_layer_exception_baseline_only_shrinks(self) -> None:
        stale = sorted(LAYER_EXCEPTIONS - _layer_violations())
        self.assertFalse(
            stale,
            "stale layer exception — boundary improved, DELETE this pair: "
            + ", ".join(f"{importer} -> {target}" for importer, target in stale),
        )

    def test_application_uses_declared_component_entrypoints(self) -> None:
        """Cross-component application imports use a facade or explicit port."""
        offenders: list[str] = []
        public_components = (RESEARCH_CORE, ARTIFACTS, SANDBOX, FEED)
        for importer, target in sorted(_import_pairs()):
            if _component(importer) != APPLICATION_COMPONENT:
                continue
            target_component = _component(target)
            if target_component not in public_components:
                continue
            root = target_component + "/"
            relative_target = target.removeprefix(root)
            if relative_target == "facade.py" or relative_target.startswith("ports/"):
                continue
            offenders.append(f"{importer} -> {target}")
        self.assertFalse(
            offenders,
            "application code must enter business components through facade.py "
            "or ports/**: " + ", ".join(offenders),
        )

    def test_composite_reads_are_application_owned_and_surface_delegates(self) -> None:
        queries = (BACKEND_ROOT / "application/queries.py").read_text(encoding="utf-8")
        workflow = (BACKEND_ROOT / "application/workflow.py").read_text(encoding="utf-8")
        control = (BACKEND_ROOT / "surface/control/control_app.py").read_text(
            encoding="utf-8"
        )
        views = (BACKEND_ROOT / "surface/transport/api/views.py").read_text(
            encoding="utf-8"
        )
        routes = "\n".join(
            (BACKEND_ROOT / f"surface/transport/api/{name}.py").read_text(
                encoding="utf-8"
            )
            for name in ("experiments", "projects")
        )
        for query in ("MlflowOverviewQuery", "ExperimentFigureQuery"):
            with self.subTest(query=query):
                self.assertIn(f"class {query}:", queries)
                self.assertIn(query, control)
        for query in ("WorkflowQuery", "ProjectDashboardQuery"):
            with self.subTest(query=query):
                self.assertIn(f"class {query}:", workflow)
                self.assertIn(query, control)
        for escaped_policy in (
            "build_experiment_figure",
            "mlflow_experiment_name",
            "ACTIVE_SANDBOX_STATUSES",
        ):
            self.assertNotIn(escaped_policy, views)
        for delegate in ("home_query(", "mlflow_overview_query(", "experiment_figure_query("):
            self.assertIn(delegate, routes)


if __name__ == "__main__":
    unittest.main()
