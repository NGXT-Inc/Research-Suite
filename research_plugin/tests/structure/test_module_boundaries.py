"""Module-boundary lints for the modular-monolith transition.

docs/MODULE_BOUNDARIES.md defines the target: a KERNEL, five modules
(RESEARCH_CORE, ARTIFACTS, OBJECT_STORAGE, SANDBOX, FEED) plus the MLFLOW
extension, and a SURFACE that composes them. Every backend production file is
assigned to exactly one module, imports (including function-local ones — the
codebase uses them to break cycles) may only follow the allowed edges, and
today's violations are frozen in GRANDFATHERED as a monotonically shrinking
baseline: new violations fail immediately, fixed ones must be deleted.
"""

from __future__ import annotations

import ast
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
# over both). Paths are backend-relative posix.
PACKAGE_MODULES = {
    "state": KERNEL,
    "ports": KERNEL,
    "domain": RESEARCH_CORE,
    "storage": OBJECT_STORAGE,
    "services/sandbox": SANDBOX,
    "sandbox": SANDBOX,
    "execution": SANDBOX,
    "mlflow": MLFLOW,
    "tools": SURFACE,
    "transport": SURFACE,
    "composition": SURFACE,
    "control": SURFACE,
    "daemon": SURFACE,
    "dataplane": SURFACE,
}

# File-level assignments and overrides. Every backend .py file must resolve to
# a module through this table or PACKAGE_MODULES — unknown files fail
# test_every_backend_file_is_classified.
FILE_MODULES = {
    # kernel: package root docstring/version shell plus shared primitives.
    "__init__.py": KERNEL,
    "utils.py": KERNEL,
    "env.py": KERNEL,
    "version.py": KERNEL,
    # secret_tokens is a pure-stdlib token helper imported by state/store.py
    # (kernel) and services/reviews.py — it must live at the kernel floor.
    "secret_tokens.py": KERNEL,
    # object_storage: blob persistence adapters carved out of state/.
    "state/blobs.py": OBJECT_STORAGE,
    "state/s3_blobs.py": OBJECT_STORAGE,
    "domain/storage_guidance.py": OBJECT_STORAGE,
    # research_core: workflow/claims/experiments/reviews/synthesis services.
    "services/workflow.py": RESEARCH_CORE,
    "services/workflow_views.py": RESEARCH_CORE,
    "services/experiments.py": RESEARCH_CORE,
    "services/experiment_views.py": RESEARCH_CORE,
    "services/claims.py": RESEARCH_CORE,
    "services/reviews.py": RESEARCH_CORE,
    "services/review_gate.py": RESEARCH_CORE,
    "services/syntheses.py": RESEARCH_CORE,
    "services/project_overview.py": RESEARCH_CORE,
    "services/projects.py": RESEARCH_CORE,
    "services/reflection_tools.py": RESEARCH_CORE,
    "services/graph_refs.py": RESEARCH_CORE,
    # artifacts: resource records, pinned bytes, figure projections.
    "services/resources.py": ARTIFACTS,
    "services/pinned.py": ARTIFACTS,
    "services/figure_view.py": ARTIFACTS,
    "domain/resource_selection.py": ARTIFACTS,
    # sandbox: lifecycle services + local strays of the sandbox stack.
    "services/transcript_cache.py": SANDBOX,
    "services/quotas.py": SANDBOX,  # TODO tenancy concern — may move later
    "domain/sandbox_paths.py": SANDBOX,
    "domain/quota_contract.py": SANDBOX,
    # ssh_keys generates sandbox ssh keypairs (sandbox_conn + mgmt keys).
    "ssh_keys.py": SANDBOX,
    # feed: services plus feed's own domain policy files.
    "services/feed.py": FEED,
    "services/feed_unfurl.py": FEED,
    "domain/feed_images.py": FEED,
    "domain/feed_policy.py": FEED,
    # surface: composition/transport strays and cross-module glue services.
    "services/__init__.py": SURFACE,  # import-free shell (test_plane_layout)
    "services/cleanup.py": SURFACE,
    "services/identity.py": SURFACE,
    "services/permissions.py": SURFACE,
    "app.py": SURFACE,
    "config.py": SURFACE,
    "client_cli.py": SURFACE,
    "local_runtime.py": SURFACE,
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

# Frozen baseline of today's violating (importer_file, imported_file) pairs.
# This list may only shrink: a fixed edge must be deleted here, and no new
# edge may be added. Move the code, not the line.
GRANDFATHERED = frozenset({
    # artifacts -> research_core (3)
    ("services/resources.py", "domain/markdown_images.py"),
    ("services/resources.py", "domain/reflection_projection.py"),
    ("services/resources.py", "domain/vocabulary.py"),
    # artifacts -> sandbox (1)
    ("services/figure_view.py", "sandbox/sandbox_support.py"),
    # kernel -> research_core (1)
    ("state/tool_calls.py", "domain/tool_call_stats.py"),
    # kernel -> sandbox (3)
    ("ports/quota_admission.py", "domain/quota_contract.py"),
    ("state/mgmt_keys.py", "sandbox/sandbox_support.py"),
    ("state/mgmt_keys.py", "ssh_keys.py"),
    # mlflow -> surface (1)
    ("mlflow/tracking.py", "config.py"),
    # research_core -> object_storage (4)
    ("services/experiments.py", "state/blobs.py"),
    ("services/reviews.py", "state/blobs.py"),
    ("services/syntheses.py", "state/blobs.py"),
    ("services/workflow.py", "domain/storage_guidance.py"),
    # sandbox -> object_storage (2)
    ("services/sandbox/sandbox_views.py", "domain/storage_guidance.py"),
    ("services/sandbox/sandboxes.py", "domain/storage_guidance.py"),
    # sandbox -> research_core (1)
    ("domain/sandbox_paths.py", "domain/paths.py"),
    # sandbox -> surface (1)
    ("services/sandbox/sandbox_daemons.py", "config.py"),
})


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
    """Absolute dotted module name -> backend-relative file path."""
    index: dict[str, str] = {}
    for path in _backend_files():
        rel = path.relative_to(BACKEND_ROOT)
        parts = rel.parent.parts if rel.name == "__init__.py" else (*rel.parent.parts, rel.stem)
        index[".".join(("backend", *parts))] = rel.as_posix()
    return index


def _import_targets(path: Path, dotted: dict[str, str]) -> set[str]:
    """Backend files imported by ``path``, top-level and function-local alike.

    Relative imports resolve against the importing file's package; for
    ``from base import name`` the deeper ``base.name`` submodule wins when it
    exists, otherwise the edge points at ``base`` itself.
    """
    rel = path.relative_to(BACKEND_ROOT)
    package = ("backend", *rel.parent.parts)
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
            "new backend files must be assigned a module in "
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

    def test_grandfathered_baseline_only_shrinks(self) -> None:
        stale = sorted(GRANDFATHERED - _current_violations())
        self.assertFalse(
            stale,
            "stale baseline entry — boundary improved, DELETE this line to ratchet: "
            + ", ".join(f"{importer} -> {target}" for importer, target in stale),
        )


if __name__ == "__main__":
    unittest.main()
