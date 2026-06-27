"""Pins the SandboxService decomposition: a thin facade over its collaborators.

Behavior is covered by test_sandbox_service.py; this file guards the
*structure* so the machinery doesn't quietly grow back into the facade:
the facade owns the public verbs and delegates rows to SandboxRegistry,
jobs/reconcile to SandboxProvisioner, every local-IO duty (conn files,
local paths, metrics fallback) to the DataPlaneWorker, and the background
loops to SandboxDaemons.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import get_type_hints

from backend.app import ResearchPluginApp
from backend.dataplane import InProcessTaskChannel, LocalDataPlaneWorker
from backend.execution.backends.fake import FakeSandboxBackend
from backend.services.sandbox.sandbox_daemons import SandboxDaemons
from backend.services.sandbox.sandbox_heartbeat import (
    SandboxHeartbeatMonitor,
    SandboxIdlePolicy,
)
from backend.services.sandbox.sandbox_metrics import SandboxMetrics
from backend.services.sandbox.sandbox_provisioner import SandboxProvisioner
from backend.services.sandbox.sandbox_registry import SandboxRegistry
from backend.services.sandbox.sandboxes import SandboxService
from backend.utils import ValidationError
from tests.paths import SERVICES_ROOT

FACADE = SERVICES_ROOT / "sandbox" / "sandboxes.py"


def _import_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module != "__future__":
                modules.add(node.module)
    return modules


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

    def test_facade_wires_the_collaborators(self) -> None:
        service = self.app.sandboxes
        self.assertIsInstance(service.registry, SandboxRegistry)
        self.assertIsInstance(service.provisioner, SandboxProvisioner)
        self.assertIsInstance(service.worker, LocalDataPlaneWorker)
        self.assertIsInstance(service.metrics, SandboxMetrics)
        self.assertIsInstance(service.daemons, SandboxDaemons)
        self.assertIsInstance(service.daemons.heartbeat, SandboxHeartbeatMonitor)
        self.assertIsInstance(service.daemons.heartbeat.policy, SandboxIdlePolicy)
        # Terminal row marks tear down conn files through the hook,
        # so the registry itself stays persistence-only.
        self.assertIsNotNone(service.registry.on_terminal)
        # All collaborators share the one registry (single writer of rows) and
        # the one worker (single owner of local IO).
        self.assertIs(service.provisioner.registry, service.registry)
        self.assertIs(service.daemons.registry, service.registry)
        self.assertIs(service.daemons.heartbeat.registry, service.registry)
        self.assertIs(service.metrics.registry, service.registry)
        self.assertIs(service.metrics.worker, service.worker)
        self.assertIs(service.worker, self.app.worker)

    def test_facade_wires_the_task_seam(self) -> None:
        # One channel carries explicit control→data signals: conn refresh and
        # teardown. It is injected by composition so split mode can swap HTTP in.
        service = self.app.sandboxes
        self.assertIsInstance(service.tasks, InProcessTaskChannel)
        self.assertIs(service.tasks.worker, service.worker)
        self.assertIs(service.metrics_archive, service.worker.metrics_archive)
        self.assertIs(service.quotas, self.app.quotas)

    def test_facade_requires_explicit_task_channel(self) -> None:
        with self.assertRaisesRegex(ValidationError, "task_channel is required"):
            SandboxService(
                store=self.app.store,
                sandbox_backend=self.app.execution_backend,
                worker=self.app.worker,
                mgmt_keys=self.app.sandboxes.mgmt_keys,
                metrics_archive=self.app.sandboxes.metrics_archive,
            )

    def test_facade_requires_task_channel_submit(self) -> None:
        with self.assertRaisesRegex(ValidationError, "task_channel.submit is required"):
            SandboxService(
                store=self.app.store,
                sandbox_backend=self.app.execution_backend,
                worker=self.app.worker,
                mgmt_keys=self.app.sandboxes.mgmt_keys,
                metrics_archive=self.app.sandboxes.metrics_archive,
                task_channel=object(),
            )

    def test_facade_requires_explicit_quota_admission(self) -> None:
        with self.assertRaisesRegex(ValidationError, "quotas is required"):
            SandboxService(
                store=self.app.store,
                sandbox_backend=self.app.execution_backend,
                worker=self.app.worker,
                mgmt_keys=self.app.sandboxes.mgmt_keys,
                metrics_archive=self.app.sandboxes.metrics_archive,
                task_channel=self.app.sandboxes.tasks,
            )

    def test_facade_requires_quota_admission_port(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "quotas.check_admission is required"
        ):
            SandboxService(
                store=self.app.store,
                sandbox_backend=self.app.execution_backend,
                worker=self.app.worker,
                mgmt_keys=self.app.sandboxes.mgmt_keys,
                metrics_archive=self.app.sandboxes.metrics_archive,
                task_channel=self.app.sandboxes.tasks,
                quotas=object(),
            )

    def test_facade_source_keeps_no_extracted_machinery(self) -> None:
        source = FACADE.read_text(encoding="utf-8")
        # Job/daemon threads live in the provisioner and daemons modules.
        self.assertNotIn("threading.Thread(", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("httpx", source)
        # Local IO (conn files and local paths) lives behind the worker.
        self.assertNotIn("SandboxConnFiles", source)
        self.assertNotIn("ssh_rsync", source)
        self.assertNotIn("SshRsyncSyncer", source)
        self.assertNotIn("InProcessTaskChannel(", source)
        self.assertNotIn("experiments", _import_modules(FACADE))
        self.assertNotIn("quotas", _import_modules(FACADE))
        self.assertNotIn("QuotaService", source)
        self.assertNotIn("dataplane.tasks", _import_modules(FACADE))
        self.assertNotIn(
            "dataplane.tasks",
            _import_modules(FACADE.parent / "sandbox_provisioner.py"),
        )
        self.assertNotIn("dataplane.worker", _import_modules(FACADE))
        self.assertNotIn(
            "dataplane.worker",
            _import_modules(FACADE.parent / "sandbox_provisioner.py"),
        )
        self.assertNotIn(
            "sync_sessions",
            _import_modules(FACADE.parent / "sandbox_provisioner.py"),
        )
        # Control-owned collaborators are injected explicitly by composition;
        # the facade must not derive them from the local worker.
        self.assertNotIn("worker.workspace", source)
        self.assertNotIn("worker.metrics_archive", source)
        self.assertNotIn("worker.client_id()", source)
        self.assertNotIn("_metrics_cache", source)
        self.assertNotIn("_metrics_lock", source)
        self.assertNotIn("_metrics_persisted_at", source)
        self.assertNotIn("def _persist_metrics_row", source)
        self.assertNotIn("def _sample_metrics_cached", source)
        self.assertIn("self.metrics.persist_row", source)
        self.assertNotIn("presign_put", source)
        self.assertNotIn("presign_get", source)
        self.assertNotIn("finalize_put", source)
        self.assertNotIn("run_parachute", source)
        self.assertNotIn("PARACHUTE_", source)
        self.assertNotIn("self.parachute", source)
        # Row SQL lives in SandboxRegistry. The conn-scoped view helper for the
        # workflow layer is the only SELECT allowed to remain.
        self.assertNotIn("UPDATE sandboxes", source)
        self.assertNotIn("INSERT INTO sandboxes", source)
        self.assertEqual(source.count("SELECT * FROM sandboxes"), 1)

    def test_facade_import_does_not_load_data_plane_task_machinery(self) -> None:
        code = """
import sys
import backend.services.sandbox.sandboxes
for name in (
    "backend.dataplane.tasks",
	    "backend.dataplane.worker",
	    "backend.dataplane.metrics_archive",
	    "backend.workspace",
):
    if name in sys.modules:
        raise SystemExit(f"{name} loaded")
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(FACADE.parents[3])
        subprocess.run([sys.executable, "-c", code], check=True, env=env)

    def test_service_type_hints_resolve_without_data_plane_worker(self) -> None:
        facade_hints = get_type_hints(SandboxService.__init__)
        provisioner_hints = get_type_hints(SandboxProvisioner.__init__)

        self.assertEqual(facade_hints["worker"].__name__, "SandboxWorker")
        self.assertNotIn("worker", provisioner_hints)

    def test_registry_module_stays_dependency_free(self) -> None:
        # The registry must not import its consumers (no cycles, no backend,
        # no local paths — rows are cloud-bound records).
        source = (FACADE.parent / "sandbox_registry.py").read_text(encoding="utf-8")
        for forbidden in (
            "sandbox_provisioner",
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
