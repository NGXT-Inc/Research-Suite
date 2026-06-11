"""Pins the SandboxService decomposition: a thin facade over four collaborators.

Behavior is covered by test_sandbox_service.py; this file guards the
*structure* so the machinery doesn't quietly grow back into the facade:
the facade owns the public verbs and delegates rows to SandboxRegistry,
jobs/reconcile to SandboxProvisioner, tunnels to DashboardTunnels, and the
background loops to SandboxDaemons.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.sandbox_daemons import SandboxDaemons
from backend.services.sandbox_dashboards import DashboardTunnels
from backend.services.sandbox_provisioner import SandboxProvisioner
from backend.services.sandbox_registry import SandboxRegistry
from tests.paths import SERVICES_ROOT

FACADE = SERVICES_ROOT / "sandboxes.py"


class SandboxDecompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=repo,
            db_path=repo / ".research_plugin" / "state.sqlite",
            execution_backend=FakeSandboxBackend(),
        )

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def test_facade_wires_the_four_collaborators(self) -> None:
        service = self.app.sandboxes
        self.assertIsInstance(service.registry, SandboxRegistry)
        self.assertIsInstance(service.provisioner, SandboxProvisioner)
        self.assertIsInstance(service.dashboards, DashboardTunnels)
        self.assertIsInstance(service.daemons, SandboxDaemons)
        # Terminal row marks tear down tunnels + conn files through the hook,
        # so the registry itself stays persistence-only.
        self.assertIsNotNone(service.registry.on_terminal)
        # All collaborators share the one registry (single writer of rows).
        self.assertIs(service.provisioner.registry, service.registry)
        self.assertIs(service.dashboards.registry, service.registry)
        self.assertIs(service.daemons.registry, service.registry)

    def test_facade_source_keeps_no_extracted_machinery(self) -> None:
        source = FACADE.read_text(encoding="utf-8")
        # Job/daemon threads live in the provisioner and daemons modules.
        self.assertNotIn("threading.Thread(", source)
        # Tunnel subprocesses and MLflow probing live in sandbox_dashboards.
        self.assertNotIn("subprocess", source)
        self.assertNotIn("httpx", source)
        # Row SQL lives in SandboxRegistry. The two conn-scoped view helpers
        # for the workflow layer are the only SELECTs allowed to remain.
        self.assertNotIn("UPDATE sandboxes", source)
        self.assertNotIn("INSERT INTO sandboxes", source)
        self.assertEqual(source.count("SELECT * FROM sandboxes"), 3)

    def test_registry_module_stays_dependency_free(self) -> None:
        # The registry must not import its consumers (no cycles, no backend).
        source = (FACADE.parent / "sandbox_registry.py").read_text(encoding="utf-8")
        for forbidden in (
            "sandbox_provisioner",
            "sandbox_dashboards",
            "sandbox_daemons",
            "import sandboxes",
            "from .sandboxes",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
