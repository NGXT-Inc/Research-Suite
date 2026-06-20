"""Pins the SandboxService decomposition: a thin facade over its collaborators.

Behavior is covered by test_sandbox_service.py; this file guards the
*structure* so the machinery doesn't quietly grow back into the facade:
the facade owns the public verbs and delegates rows to SandboxRegistry,
jobs/reconcile to SandboxProvisioner, every local-IO duty (conn files, rsync,
dashboard tunnels) to the DataPlaneWorker, and the background loops to
SandboxDaemons.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app import ResearchPluginApp
from backend.dataplane import InProcessTaskChannel, LocalDataPlaneWorker
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.sandbox_daemons import SandboxDaemons
from backend.services.sandbox_dashboards import DashboardTunnels
from backend.services.sandbox_provisioner import SandboxProvisioner
from backend.services.sandbox_registry import SandboxRegistry
from backend.services.sandboxes import SandboxService
from backend.services.sync_sessions import (
    InProcessControlPlaneView,
    LeaseService,
    SyncSessionService,
)
from backend.utils import ValidationError
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
        self.assertIsInstance(service.worker, LocalDataPlaneWorker)
        self.assertIsInstance(service.worker.dashboards, DashboardTunnels)
        self.assertIsInstance(service.daemons, SandboxDaemons)
        # Terminal row marks tear down tunnels + conn files through the hook,
        # so the registry itself stays persistence-only.
        self.assertIsNotNone(service.registry.on_terminal)
        # All collaborators share the one registry (single writer of rows) and
        # the one worker (single owner of local IO).
        self.assertIs(service.provisioner.registry, service.registry)
        self.assertIs(service.provisioner.worker, service.worker)
        self.assertIs(service.daemons.registry, service.registry)
        self.assertIs(service.worker, self.app.worker)
        # Data-plane work that deserves a record (a tunnel came up) reports
        # through the registry's event stream, not its own writes.
        self.assertIsNotNone(service.worker.dashboards.emit_event)

    def test_facade_wires_the_phase4_seam(self) -> None:
        # Sync sessions, leases, and the task channel (cloud plan Phase 4):
        # one lease service backs the session issuer and the poller's view,
        # one channel carries every control→data signal, and the provisioner
        # shares both rather than minting its own.
        service = self.app.sandboxes
        self.assertIsInstance(service.leases, LeaseService)
        self.assertIsInstance(service.sessions, SyncSessionService)
        self.assertIsInstance(service.tasks, InProcessTaskChannel)
        self.assertIsInstance(service.control_view, InProcessControlPlaneView)
        self.assertIs(service.sessions.leases, service.leases)
        self.assertIs(service.provisioner.sessions, service.sessions)
        self.assertIs(service.provisioner.tasks, service.tasks)
        self.assertIs(service.control_view.registry, service.registry)
        self.assertIs(service.control_view.sessions, service.sessions)
        self.assertIs(service.daemons.view, service.control_view)
        self.assertIs(service.tasks.worker, service.worker)
        self.assertIs(service.metrics_archive, service.worker.metrics_archive)
        # Local composition injects the worker-backed values; the facade itself
        # must not derive them from worker internals (pinned by source test).
        self.assertEqual(service.sessions.client_id, service.worker.client_id())

    def test_facade_requires_explicit_lease_client_id(self) -> None:
        with self.assertRaisesRegex(ValidationError, "lease_client_id is required"):
            SandboxService(
                store=self.app.store,
                sandbox_backend=self.app.execution_backend,
                worker=self.app.worker,
                mgmt_keys=self.app.sandboxes.mgmt_keys,
                metrics_archive=self.app.sandboxes.metrics_archive,
                lease_client_id="",
            )

    def test_facade_source_keeps_no_extracted_machinery(self) -> None:
        source = FACADE.read_text(encoding="utf-8")
        # Job/daemon threads live in the provisioner and daemons modules.
        self.assertNotIn("threading.Thread(", source)
        # Tunnel subprocesses and MLflow probing live in sandbox_dashboards.
        self.assertNotIn("subprocess", source)
        self.assertNotIn("httpx", source)
        # Local IO (conn files, rsync, tunnels) lives behind the worker.
        self.assertNotIn("SandboxConnFiles", source)
        self.assertNotIn("ssh_rsync", source)
        self.assertNotIn("SshRsyncSyncer", source)
        # Control-owned collaborators are injected explicitly by composition;
        # the facade must not derive them from the local worker.
        self.assertNotIn("worker.workspace", source)
        self.assertNotIn("worker.metrics_archive", source)
        self.assertNotIn("worker.client_id()", source)
        # Row SQL lives in SandboxRegistry. The two conn-scoped view helpers
        # for the workflow layer are the only SELECTs allowed to remain.
        self.assertNotIn("UPDATE sandboxes", source)
        self.assertNotIn("INSERT INTO sandboxes", source)
        self.assertEqual(source.count("SELECT * FROM sandboxes"), 3)

    def test_registry_module_stays_dependency_free(self) -> None:
        # The registry must not import its consumers (no cycles, no backend,
        # no local paths — rows are cloud-bound records).
        source = (FACADE.parent / "sandbox_registry.py").read_text(encoding="utf-8")
        for forbidden in (
            "sandbox_provisioner",
            "sandbox_dashboards",
            "sandbox_daemons",
            "import sandboxes",
            "from .sandboxes",
            "workspace",
            "repo_root",
        ):
            self.assertNotIn(forbidden, source)

    def test_views_module_stays_pure_projection(self) -> None:
        # The agent-view decomposition (cloud plan §3.3): row facts are pure;
        # conn files and local paths arrive as worker enrichment. The views
        # module must not grow them back.
        source = (FACADE.parent / "sandbox_views.py").read_text(encoding="utf-8")
        for forbidden in ("sandbox_conn", "subprocess", "repo_root", "workspace"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
