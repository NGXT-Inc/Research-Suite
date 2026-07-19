"""Module-boundary lints for the modular-monolith transition.

docs/MODULE_BOUNDARIES.md defines the target: a KERNEL, five modules
(RESEARCH_CORE, ARTIFACTS, OBJECT_STORAGE, SANDBOX, FEED) plus the MLFLOW
extension, and a SURFACE that composes them. Every brain production file is
assigned to exactly one module, imports (including function-local ones — the
codebase uses them to break cycles) may only follow the allowed edges, and
today's violations are frozen in GRANDFATHERED as a monotonically shrinking
baseline: new violations fail immediately, fixed ones must be deleted.
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
SURFACE = "surface"

MODULES = (KERNEL, RESEARCH_CORE, ARTIFACTS, OBJECT_STORAGE, SANDBOX, FEED, MLFLOW, SURFACE)

# Directory-level assignments (deepest matching prefix wins; FILE_MODULES wins
# over both). Paths are brain-relative posix.
PACKAGE_MODULES = {
    "kernel": KERNEL,
    "research_core": RESEARCH_CORE,
    "artifacts": ARTIFACTS,
    "object_storage": OBJECT_STORAGE,
    "sandbox": SANDBOX,
    "feed": FEED,
    "mlflow": MLFLOW,
    "tools": SURFACE,
    "transport": SURFACE,
    "composition": SURFACE,
    "control": SURFACE,
    "dataplane": SURFACE,
    # cross-module glue services (auth/cleanup/identity/permissions) awaiting
    # the surface sub-foldering tranche.
    "services": SURFACE,
}

# File-level assignments and overrides. Every brain .py file must resolve to
# a module through this table or PACKAGE_MODULES — unknown files fail
# test_every_backend_file_is_classified.
FILE_MODULES = {
    # kernel: package root docstring/version shell.
    "__init__.py": KERNEL,
    # surface: composition/transport strays awaiting the surface tranche.
    "config.py": SURFACE,
    "client_cli.py": SURFACE,
    "observability.py": SURFACE,
    "workspace.py": SURFACE,
}

# The import law: kernel imports only kernel; each module imports itself +
# kernel; surface imports anything; NOTHING imports surface. Plus three
# ratified module-to-module allowances (see docs/MODULE_BOUNDARIES.md).
ALLOWED_EDGES = (
    {(module, module) for module in MODULES}
    | {(module, KERNEL) for module in MODULES}
    | {(SURFACE, module) for module in MODULES}
    | {
        (RESEARCH_CORE, ARTIFACTS),  # workflow gates judge pinned bytes
        (ARTIFACTS, OBJECT_STORAGE),  # resource versions persist blobs
        (FEED, OBJECT_STORAGE),  # feed images persist blobs
        (MLFLOW, RESEARCH_CORE),  # extension reads experiment records
    }
)

# Frozen baseline of violating (importer_file, imported_file) pairs.
# This list may only shrink: a fixed edge must be deleted here, and no new
# edge may be added. Move the code, not the line. Phase 4a drove it to ZERO —
# every import now follows the law above; keep it that way.
GRANDFATHERED: frozenset[tuple[str, str]] = frozenset()


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
SQL_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)\b", re.IGNORECASE
)


def _backend_files() -> list[Path]:
    return sorted(
        path
        for path in BACKEND_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _classify(rel: str) -> str | None:
    if rel in FILE_MODULES:
        return FILE_MODULES[rel]
    parts = rel.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        prefix = "/".join(parts[:depth])
        if prefix in PACKAGE_MODULES:
            return PACKAGE_MODULES[prefix]
    return None


def _dotted_index() -> dict[str, str]:
    """Absolute dotted module name -> brain-relative file path."""
    index: dict[str, str] = {}
    for path in _backend_files():
        rel = path.relative_to(BACKEND_ROOT)
        parts = rel.parent.parts if rel.name == "__init__.py" else (*rel.parent.parts, rel.stem)
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


def _current_violations() -> set[tuple[str, str]]:
    dotted = _dotted_index()
    violations: set[tuple[str, str]] = set()
    for path in _backend_files():
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        importer = _classify(rel)
        for target in _import_targets(path, dotted):
            if target == rel:
                continue
            if (importer, _classify(target)) not in ALLOWED_EDGES:
                violations.add((rel, target))
    return violations


class ModuleBoundaryTest(unittest.TestCase):
    def test_every_backend_file_is_classified(self) -> None:
        unclassified = sorted(
            rel
            for path in _backend_files()
            if _classify(rel := path.relative_to(BACKEND_ROOT).as_posix()) is None
        )
        self.assertFalse(
            unclassified,
            "new brain files must be assigned a module in "
            f"tests/structure/test_module_boundaries.py: {unclassified}",
        )

    def test_classification_tables_carry_no_stale_paths(self) -> None:
        for rel in sorted(FILE_MODULES):
            with self.subTest(file=rel):
                self.assertTrue((BACKEND_ROOT / rel).is_file(), f"stale FILE_MODULES entry: {rel}")
        for prefix in sorted(PACKAGE_MODULES):
            with self.subTest(package=prefix):
                self.assertTrue((BACKEND_ROOT / prefix).is_dir(), f"stale PACKAGE_MODULES entry: {prefix}")

    def test_no_new_module_boundary_violations(self) -> None:
        new = sorted(_current_violations() - GRANDFATHERED)
        self.assertFalse(
            new,
            "new module-boundary violation (see docs/MODULE_BOUNDARIES.md): "
            + ", ".join(
                f"{importer} -> {target} [{_classify(importer)} -> {_classify(target)}]"
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
            module = _classify(rel)
            if module in (None, KERNEL, SURFACE):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Constant) and isinstance(node.value, str)
                ):
                    continue
                for match in SQL_TABLE_REF.finditer(node.value):
                    owner = TABLE_OWNERS.get(match.group(1).lower())
                    if owner is None or owner == module:
                        continue
                    if (module, owner) not in ALLOWED_EDGES:
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

    def test_grandfathered_baseline_only_shrinks(self) -> None:
        stale = sorted(GRANDFATHERED - _current_violations())
        self.assertFalse(
            stale,
            "stale baseline entry — boundary improved, DELETE this line to ratchet: "
            + ", ".join(f"{importer} -> {target}" for importer, target in stale),
        )


if __name__ == "__main__":
    unittest.main()
