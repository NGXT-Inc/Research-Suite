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
from collections import Counter
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
    | {
        (OBJECT_STORAGE, dependency)
        for dependency in (OBJECT_STORAGE, APPLICATION_COMPONENT, KERNEL)
    }
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
    "artifacts/ports": PORT,
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
    "artifacts/association_policy.py": DOMAIN,
    "feed/feed_policy.py": DOMAIN,
    "feed/feed_unfurl.py": ADAPTER,
    "feed/ports.py": PORT,
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
    "surface/project_keys.py": APPLICATION_LAYER,
    "surface/project_key_store.py": ADAPTER,
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
LAYER_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset()

# SQL follows the import law: a module may name its own tables, Kernel tables,
# and tables behind ratified component edges. Every stable table is explicit;
# temporary ``*_migrate`` rebuild tables are ignored by the ownership check.
TABLE_OWNERS = {
    "projects": KERNEL,
    "project_members": KERNEL,
    "project_api_keys": SURFACE,
    "events": KERNEL,
    "schema_migrations": KERNEL,
    "tenants": KERNEL,
    "tool_calls": KERNEL,
    "experiments": RESEARCH_CORE,
    "experiment_claims": RESEARCH_CORE,
    "claims": RESEARCH_CORE,
    "reviews": RESEARCH_CORE,
    "review_requests": RESEARCH_CORE,
    "review_sessions": RESEARCH_CORE,
    "reflections": RESEARCH_CORE,
    "reflection_claim_changes": RESEARCH_CORE,
    "reflection_experiments": RESEARCH_CORE,
    "litreview_sections": RESEARCH_CORE,
    "papers": RESEARCH_CORE,
    "paper_links": RESEARCH_CORE,
    "artifacts": ARTIFACTS,
    "artifact_figures": ARTIFACTS,
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
    "feed_upload_tokens": FEED,
}
SQL_TABLE_REF = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)\b", re.IGNORECASE)
CREATE_TABLE_REF = re.compile(
    r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([a-z_]+)\s*\(",
    re.IGNORECASE,
)
FOREIGN_SQL_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM|REFERENCES)\s+([a-z_]+)\b",
    re.IGNORECASE,
)

RESEARCH_ARTIFACT_SQL_BASELINE: Counter[tuple[str, str, str]] = Counter()

APPLICATION_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "boto3",
        "django",
        "dotenv",
        "fastapi",
        "flask",
        "httpx",
        "mlflow",
        "modal",
        "os",
        "psycopg",
        "pydantic",
        "requests",
        "socket",
        "sqlalchemy",
        "sqlite3",
        "starlette",
        "subprocess",
        "urllib",
        "uvicorn",
    }
)
APPLICATION_SQL = re.compile(
    r"\b(?:SELECT\b[\s\S]{0,300}?\bFROM|INSERT\s+INTO|"
    r"UPDATE\s+[a-z_]+\s+SET|DELETE\s+FROM|"
    r"(?:CREATE|ALTER|DROP)\s+TABLE)\b",
    re.IGNORECASE,
)
CONCRETE_COLLABORATOR_SUFFIXES = (
    "Backend",
    "Client",
    "Dispatcher",
    "Facade",
    "Handler",
    "Query",
    "Reader",
    "Repository",
    "Runtime",
    "Service",
    "Store",
    "Writer",
)
CONCRETE_FACTORY_PREFIXES = ("build_", "create_", "make_")
CONCRETE_FACTORY_SUFFIXES = tuple(
    f"_{suffix.lower()}" for suffix in CONCRETE_COLLABORATOR_SUFFIXES
)

DELIVERY_PERSISTENCE_MEMBERS = frozenset(
    {"store", "_store", "transaction", "connect", "cursor"}
)
DELIVERY_DYNAMIC_REACH_THROUGH_MEMBERS = (
    DELIVERY_PERSISTENCE_MEMBERS | {"__dict__"}
)
DELIVERY_WHOLE_DEPENDENCY_CARRIERS = frozenset(
    {"ControlApp", "HttpDependencies"}
)


def _is_concrete_factory(name: str) -> bool:
    return name.startswith(CONCRETE_FACTORY_PREFIXES) and name.endswith(
        CONCRETE_FACTORY_SUFFIXES
    )


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


def _public_entrypoint_violations() -> set[tuple[str, str]]:
    violations: set[tuple[str, str]] = set()
    for importer, target in _import_pairs():
        importer_component = _component(importer)
        target_component = _component(target)
        if (
            importer_component == target_component
            or target_component == KERNEL
            or _layer(importer) == BOOTSTRAP
        ):
            continue
        relative_target = target.removeprefix(f"{target_component}/")
        if relative_target == "facade.py" or relative_target.startswith("ports/"):
            continue
        violations.add((importer, target))
    return violations


def _created_tables() -> set[str]:
    tables: set[str] = set()
    for path in _backend_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                tables.update(
                    match.group(1).lower()
                    for match in CREATE_TABLE_REF.finditer(node.value)
                    if not match.group(1).lower().endswith("_migrate")
                )
    return tables


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def _research_artifact_sql() -> Counter[tuple[str, str, str]]:
    references: Counter[tuple[str, str, str]] = Counter()
    artifact_tables = {
        table for table, owner in TABLE_OWNERS.items() if owner == ARTIFACTS
    }
    for path in sorted((BACKEND_ROOT / RESEARCH_CORE).rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            for match in FOREIGN_SQL_TABLE_REF.finditer(node.value):
                table = match.group(1).lower()
                if table in artifact_tables:
                    references[(rel, _enclosing_function(node, parents), table)] += 1
    return references


def _application_purity_violations() -> list[str]:
    violations: list[str] = []
    dotted = _dotted_index()
    for path in sorted((BACKEND_ROOT / APPLICATION_COMPONENT).rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for target in _import_targets(path, dotted):
            if target.startswith("kernel/state/") or target == "kernel/env.py":
                violations.append(f"{rel}: imports state/config module {target}")
            if _component(target) in (SURFACE, MLFLOW, OBJECT_STORAGE) or _layer(
                target
            ) == ADAPTER:
                violations.append(f"{rel}: imports concrete adapter {target}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                roots = set()
            for root in roots & APPLICATION_FORBIDDEN_IMPORT_ROOTS:
                violations.append(f"{rel}:{node.lineno}: imports {root}")
            if isinstance(node, ast.Name) and node.id in {
                "BaseStateStore",
                "StateStore",
            }:
                violations.append(f"{rel}:{node.lineno}: names {node.id}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameters = (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
                for parameter in parameters:
                    if parameter.arg in {"conn", "connection", "cursor", "store"}:
                        violations.append(
                            f"{rel}:{parameter.lineno}: accepts persistence parameter "
                            f"{parameter.arg}"
                        )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"connect", "cursor", "transaction"}
            ):
                violations.append(
                    f"{rel}:{node.lineno}: calls persistence method {node.func.attr}"
                )

        docstrings = {
            id(owner.body[0].value)
            for owner in ast.walk(tree)
            if isinstance(
                owner,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and owner.body
            and isinstance(owner.body[0], ast.Expr)
            and isinstance(owner.body[0].value, ast.Constant)
            and isinstance(owner.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and APPLICATION_SQL.search(node.value)
            ):
                violations.append(f"{rel}:{node.lineno}: contains SQL")
    return sorted(set(violations))


def _delivery_boundary_violations(
    source: str, *, relative: str = "<synthetic>"
) -> list[str]:
    """Reject raw implementations and persistence reach-through in Delivery.

    This scan is deliberately structural rather than tied to today's routes.
    Attribute access is rejected at its persistence member, so it still catches
    values reached through local aliases or arbitrarily deep attribute chains.
    """
    tree = ast.parse(source, filename=relative)
    violations: set[str] = set()
    raw_aliases: set[str] = set()
    carrier_aliases = set(DELIVERY_WHOLE_DEPENDENCY_CARRIERS)

    def is_raw_type(name: str) -> bool:
        return name == "ControlApp" or name.endswith(("Service", "Store"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            bound_name = alias.asname or alias.name
            if is_raw_type(alias.name):
                raw_aliases.add(bound_name)
                violations.add(
                    f"{relative}:{node.lineno}: imports raw implementation type "
                    f"{alias.name}"
                )
            if alias.name in DELIVERY_WHOLE_DEPENDENCY_CARRIERS:
                carrier_aliases.add(bound_name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (
            is_raw_type(node.id) or node.id in raw_aliases
        ):
            violations.add(
                f"{relative}:{node.lineno}: names raw implementation type {node.id}"
            )
        elif isinstance(node, ast.Attribute):
            if node.attr in DELIVERY_DYNAMIC_REACH_THROUGH_MEMBERS:
                violations.add(
                    f"{relative}:{node.lineno}: reaches through to {node.attr}"
                )
            elif is_raw_type(node.attr):
                violations.add(
                    f"{relative}:{node.lineno}: names raw implementation type "
                    f"{node.attr}"
                )
        elif (
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "getattr"
            )
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in DELIVERY_DYNAMIC_REACH_THROUGH_MEMBERS
        ):
            violations.add(
                f"{relative}:{node.lineno}: dynamically reaches through to "
                f"{node.args[1].value}"
            )

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "build_router" and not node.name.startswith("register_"):
            continue
        parameters = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        for parameter in parameters:
            if parameter.annotation is None:
                continue
            annotation_names = {
                child.id
                for child in ast.walk(parameter.annotation)
                if isinstance(child, ast.Name)
            } | {
                child.attr
                for child in ast.walk(parameter.annotation)
                if isinstance(child, ast.Attribute)
            }
            if isinstance(parameter.annotation, ast.Constant) and isinstance(
                parameter.annotation.value, str
            ):
                annotation_names.update(
                    re.findall(r"[A-Za-z_][A-Za-z0-9_]*", parameter.annotation.value)
                )
            carriers = sorted(annotation_names & carrier_aliases)
            if carriers:
                violations.add(
                    f"{relative}:{parameter.lineno}: {node.name} receives whole "
                    f"dependency carrier {carriers[0]}"
                )

    return sorted(violations)


def _cross_component_constructions_outside_bootstrap() -> list[str]:
    """Find construction of another component's concrete collaborator."""
    dotted = _dotted_index()
    violations: list[str] = []
    for path in _backend_files():
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        if _layer(rel) == BOOTSTRAP:
            continue
        package = ("merv", "brain", *path.relative_to(BACKEND_ROOT).parent.parts)
        imported: dict[str, tuple[str, str]] = {}
        imported_modules: dict[tuple[str, ...], str] = {}
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = dotted.get(alias.name)
                    if target:
                        prefix = (
                            (alias.asname,)
                            if alias.asname
                            else tuple(alias.name.split("."))
                        )
                        imported_modules[prefix] = target
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = ".".join(package[: len(package) - (node.level - 1)])
                if node.module:
                    base = f"{base}.{node.module}"
            elif node.module:
                base = node.module
            else:
                continue
            target = dotted.get(base)
            for alias in node.names:
                candidate = dotted.get(f"{base}.{alias.name}") or target
                if candidate and (
                    alias.name.endswith(CONCRETE_COLLABORATOR_SUFFIXES)
                    or _is_concrete_factory(alias.name)
                ):
                    imported[alias.asname or alias.name] = (candidate, alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in imported:
                target, class_name = imported[node.func.id]
            elif isinstance(node.func, ast.Attribute):
                chain: list[str] = []
                current: ast.AST = node.func
                while isinstance(current, ast.Attribute):
                    chain.append(current.attr)
                    current = current.value
                if isinstance(current, ast.Name):
                    chain.append(current.id)
                parts = tuple(reversed(chain))
                target = ""
                class_name = parts[-1] if parts else ""
                for prefix, candidate in imported_modules.items():
                    if parts[:-1] == prefix:
                        target = candidate
                        break
                if not target or not (
                    class_name.endswith(CONCRETE_COLLABORATOR_SUFFIXES)
                    or _is_concrete_factory(class_name)
                ):
                    continue
            else:
                continue
            if _component(target) not in {_component(rel), KERNEL}:
                violations.append(
                    f"{rel}:{node.lineno} constructs {class_name} from {target}"
                )
    return sorted(violations)


class ModuleBoundaryTest(unittest.TestCase):
    def test_no_source_references_tracking_credentials_allowed(self) -> None:
        # The v29 per-sandbox trust column is moot under the no-dataplane
        # transition (MLflow suspension + project-shared sandboxes) and must
        # never be ported: no column, no read site, no reference anywhere.
        offenders = [
            path.relative_to(BACKEND_ROOT).as_posix()
            for path in _backend_files()
            if "tracking_credentials_allowed" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

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

    def test_every_stable_table_has_one_explicit_owner(self) -> None:
        created = _created_tables()
        unowned = sorted(created - TABLE_OWNERS.keys())
        stale = sorted(TABLE_OWNERS.keys() - created)
        self.assertFalse(
            unowned,
            "new persistent tables need an explicit component owner: "
            + ", ".join(unowned),
        )
        self.assertFalse(
            stale,
            "stale table-owner entries must be deleted: " + ", ".join(stale),
        )
        self.assertNotIn(
            APPLICATION_COMPONENT,
            TABLE_OWNERS.values(),
            "Application coordinates components and may not own persistence",
        )

    def test_layer_exception_baseline_only_shrinks(self) -> None:
        stale = sorted(LAYER_EXCEPTIONS - _layer_violations())
        self.assertFalse(
            stale,
            "stale layer exception — boundary improved, DELETE this pair: "
            + ", ".join(f"{importer} -> {target}" for importer, target in stale),
        )

    def test_cross_component_imports_use_public_entrypoints(self) -> None:
        """All non-bootstrap cross-component imports enter via facade/port."""
        violations = sorted(_public_entrypoint_violations())
        self.assertFalse(
            violations,
            "cross-component internal import; use facade.py or ports/**: "
            + ", ".join(f"{source} -> {target}" for source, target in violations),
        )

    def test_research_artifact_sql_inventory_only_shrinks(self) -> None:
        current = _research_artifact_sql()
        new = current - RESEARCH_ARTIFACT_SQL_BASELINE
        stale = RESEARCH_ARTIFACT_SQL_BASELINE - current
        self.assertFalse(
            new,
            "new Research SQL names Artifact-owned tables: "
            + ", ".join(f"{key} x{count}" for key, count in sorted(new.items())),
        )
        self.assertFalse(
            stale,
            "Research/Artifacts SQL boundary improved; lower the baseline: "
            + ", ".join(f"{key} x{count}" for key, count in sorted(stale.items())),
        )

    def test_application_layer_has_no_adapter_framework_store_or_sql_access(self) -> None:
        violations = _application_purity_violations()
        self.assertFalse(
            violations,
            "Application must remain pure orchestration over ports/facades: "
            + ", ".join(violations),
        )

    def test_delivery_has_no_raw_implementation_or_persistence_access(self) -> None:
        violations: list[str] = []
        for path in _backend_files():
            relative = path.relative_to(BACKEND_ROOT).as_posix()
            if _layer(relative) != DELIVERY:
                continue
            violations.extend(
                _delivery_boundary_violations(
                    path.read_text(encoding="utf-8"), relative=relative
                )
            )
        self.assertFalse(
            violations,
            "Delivery may use only public facades/use cases and may not reach "
            "through to persistence or whole-app dependency carriers: "
            + ", ".join(violations),
        )

    def test_delivery_boundary_scan_rejects_adversarial_reach_through(self) -> None:
        cases = {
            "raw ControlApp": (
                "from backend import ControlApp as Backend\nvalue: Backend\n",
                "raw implementation type",
            ),
            "raw service": (
                "from records import ResourceService as Records\nvalue: Records\n",
                "raw implementation type",
            ),
            "raw store": (
                "from state import BaseStateStore as Database\nvalue: Database\n",
                "raw implementation type",
            ),
            "direct persistence": (
                "def route(api):\n    return api.store\n",
                "reaches through to store",
            ),
            "private persistence": (
                "def route(api):\n    return api._store\n",
                "reaches through to _store",
            ),
            "one-hop alias": (
                "def route(api):\n"
                "    records = api.resources\n"
                "    return records.store\n",
                "reaches through to store",
            ),
            "multi-hop alias": (
                "def route(ctx):\n"
                "    api = ctx.api\n"
                "    records = api.resources\n"
                "    return records.store\n",
                "reaches through to store",
            ),
            "transaction": (
                "def route(unit):\n    return unit.transaction()\n",
                "reaches through to transaction",
            ),
            "connection": (
                "def route(database):\n    return database.connect()\n",
                "reaches through to connect",
            ),
            "cursor": (
                "def route(connection):\n    return connection.cursor()\n",
                "reaches through to cursor",
            ),
            "dynamic getattr": (
                "def route(api):\n    return getattr(api, 'store')\n",
                "dynamically reaches through to store",
            ),
            "dynamic private getattr": (
                "def route(api):\n    return getattr(api, '_store')\n",
                "dynamically reaches through to _store",
            ),
            "qualified getattr": (
                "import builtins\n"
                "def route(api):\n"
                "    return builtins.getattr(api, 'transaction')\n",
                "dynamically reaches through to transaction",
            ),
            "introspection": (
                "def route(api):\n    return api.__dict__['resources']\n",
                "reaches through to __dict__",
            ),
            "ControlApp router carrier": (
                "def build_router(app: 'ControlApp'):\n    return app\n",
                "build_router receives whole dependency carrier ControlApp",
            ),
            "aliased HTTP router carrier": (
                "from dependencies import HttpDependencies as Whole\n"
                "def build_router(dependencies: Whole):\n"
                "    return dependencies\n",
                "build_router receives whole dependency carrier Whole",
            ),
            "HTTP registrar carrier": (
                "from dependencies import HttpDependencies\n"
                "def register_routes(dependencies: HttpDependencies):\n"
                "    return dependencies\n",
                "register_routes receives whole dependency carrier HttpDependencies",
            ),
        }
        for name, (source, expected) in cases.items():
            with self.subTest(case=name):
                violations = _delivery_boundary_violations(source)
                self.assertTrue(
                    any(expected in violation for violation in violations),
                    f"scanner missed {name}: {violations}",
                )

    def test_delivery_boundary_scan_allows_narrow_public_dependencies(self) -> None:
        source = """
def build_router(ctx: ApiRouteContext, *, records: ArtifactRecords):
    def route(project_id: str):
        return records.list(project_id=project_id, cursor_token=None)
    return route
"""
        self.assertEqual(_delivery_boundary_violations(source), [])

    def test_only_bootstrap_constructs_cross_component_collaborators(self) -> None:
        violations = _cross_component_constructions_outside_bootstrap()
        self.assertFalse(
            violations,
            "construct concrete cross-component collaborators in bootstrap and "
            "inject a facade/port instead: " + ", ".join(violations),
        )

    def test_composite_reads_are_application_owned_and_surface_delegates(self) -> None:
        queries = (BACKEND_ROOT / "application/queries.py").read_text(encoding="utf-8")
        figure = (BACKEND_ROOT / "application/experiment_figure.py").read_text(
            encoding="utf-8"
        )
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
        self.assertIn("def build_experiment_figure(", figure)
        self.assertFalse((BACKEND_ROOT / "artifacts/figure_view.py").exists())
        self.assertNotIn(
            "build_experiment_figure",
            (BACKEND_ROOT / "artifacts/facade.py").read_text(encoding="utf-8"),
        )
        for query in ("StatusAndNextQuery", "ProjectDashboardQuery"):
            with self.subTest(query=query):
                self.assertIn(f"class {query}:", workflow)
                self.assertIn(query, control)
        for escaped_policy in (
            "build_experiment_figure",
            "tracking_experiment_name",
            "ACTIVE_SANDBOX_STATUSES",
        ):
            self.assertNotIn(escaped_policy, views)
        for delegate in ("dashboard(", "tracking(", "figure("):
            self.assertIn(delegate, routes)


if __name__ == "__main__":
    unittest.main()
