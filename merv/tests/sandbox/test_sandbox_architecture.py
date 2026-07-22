from __future__ import annotations

import ast
import unittest
from pathlib import Path

from merv.brain.sandbox.repository import SandboxRepository


ROOT = Path(__file__).parents[2] / "src" / "merv" / "brain"

FACADE_INTERNALS = {
    "_secrets_delivered",
    "activity_policy",
    "attachment_check",
    "backend",
    "daemons",
    "lifecycle",
    "metrics",
    "mgmt_keys",
    "provisioner",
    "queries",
    "quotas",
    "repository",
    "runs_ledger",
    "runtime",
    "runs_wait_poll_seconds",
    "store",
    "storage_enabled",
    "storage_hint",
    "tasks",
    "transcript_cache",
    "request_wait_seconds",
    "worker",
}


class SandboxArchitectureTest(unittest.TestCase):
    def test_legacy_repository_and_facade_modules_are_absent(self) -> None:
        self.assertFalse((ROOT / "sandbox" / "sandbox_registry.py").exists())
        self.assertFalse((ROOT / "sandbox" / "sandboxes.py").exists())
        self.assertEqual(SandboxRepository.__module__, "merv.brain.sandbox.repository")

    def test_sandbox_paths_have_one_canonical_module(self) -> None:
        self.assertFalse((ROOT / "sandbox" / "execution" / "sync_dirs.py").exists())

    def test_facade_does_not_construct_or_start_runtime(self) -> None:
        source = (ROOT / "sandbox" / "facade.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        facade = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SandboxFacade"
        )
        constructor_node = next(
            node
            for node in facade.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        constructor = ast.get_source_segment(source, constructor_node)
        self.assertIsNotNone(constructor)
        self.assertNotIn("SandboxRepository(", constructor)
        self.assertNotIn("SandboxProvisioner(", constructor)
        self.assertNotIn("SandboxDaemons(", constructor)
        self.assertNotIn(".start()", constructor)

    def test_composition_owns_runtime_start_and_shutdown(self) -> None:
        source = (ROOT / "surface" / "control" / "control_app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("build_sandbox_runtime(", source)
        self.assertIn("self._sandbox_runtime.start()", source)
        self.assertIn("self._sandbox_runtime.shutdown()", source)

    def test_lifecycle_reducer_is_pure(self) -> None:
        path = ROOT / "sandbox" / "lifecycle_reducer.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertFalse(
            any(
                name.endswith(("sandbox_backend", "repository", "state.store"))
                for name in imported
            )
        )

    def test_production_does_not_reach_through_sandbox_facade(self) -> None:
        offenders: list[str] = []
        for path in ROOT.rglob("*.py"):
            if path.is_relative_to(ROOT / "sandbox"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if not (isinstance(value, ast.Attribute) and value.attr == "sandboxes"):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                aliases.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in FACADE_INTERNALS
                    and (
                        isinstance(node.value, ast.Attribute)
                        and node.value.attr == "sandboxes"
                        or isinstance(node.value, ast.Name)
                        and node.value.id in aliases
                    )
                ):
                    offenders.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno} "
                        f".sandboxes.{node.attr}"
                    )
        self.assertFalse(
            offenders,
            "use a Sandbox facade method or a composition-owned runtime: "
            + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
