from __future__ import annotations

import ast
import unittest
from pathlib import Path

from merv.brain.sandbox.repository import SandboxRepository
from merv.brain.sandbox.sandbox_registry import SandboxRegistry


ROOT = Path(__file__).parents[2] / "src" / "merv" / "brain"


class SandboxArchitectureTest(unittest.TestCase):
    def test_repository_is_the_compatibility_registry(self) -> None:
        self.assertIs(SandboxRepository, SandboxRegistry)

    def test_facade_does_not_construct_or_start_runtime(self) -> None:
        source = (ROOT / "sandbox" / "sandboxes.py").read_text(encoding="utf-8")
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
        self.assertIn("self.sandbox_runtime.start()", source)
        self.assertIn("self.sandbox_runtime.shutdown()", source)

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
                name.endswith(("sandbox_backend", "sandbox_registry", "state.store"))
                for name in imported
            )
        )


if __name__ == "__main__":
    unittest.main()
