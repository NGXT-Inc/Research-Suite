"""Pins the SandboxService decomposition: a thin facade over its collaborators.

Behavior is covered by test_sandbox_service.py; this file guards the
*structure* so the machinery doesn't quietly grow back into the facade:
the facade owns the public verbs and delegates rows to SandboxRegistry,
job threads to SandboxProvisioner, every destructive decision (liveness
policy, VM termination, terminal marks + teardown, reconcile, reaping) to
SandboxLifecycle — the single owner of status transitions — control-owned task
signals to the neutral ControlTaskChannel, and the background loops to
SandboxDaemons.
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

from tests.support.brain import TestBrain
from merv.brain.surface.control.control_runtime import (
    ControlSandboxWorker,
    ControlTaskChannel,
)
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.sandbox.sandbox_daemons import SandboxDaemons
from merv.brain.sandbox.sandbox_heartbeat import (
    SandboxHeartbeatMonitor,
    SandboxIdlePolicy,
)
from merv.brain.sandbox.sandbox_lifecycle import SandboxLifecycle
from merv.brain.sandbox.sandbox_metrics import SandboxMetrics
from merv.brain.sandbox.sandbox_provisioner import SandboxProvisioner
from merv.brain.sandbox.sandbox_registry import SandboxRegistry
from merv.brain.sandbox.sandboxes import SandboxService
from merv.brain.kernel.utils import ValidationError
from tests.paths import BACKEND_ROOT, IMPORT_ROOT

FACADE = BACKEND_ROOT / "sandbox" / "sandboxes.py"


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
        self.app = TestBrain(
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
        self.assertIsInstance(service.lifecycle, SandboxLifecycle)
        self.assertIsInstance(service.provisioner, SandboxProvisioner)
        self.assertIsInstance(service.worker, ControlSandboxWorker)
        self.assertIsInstance(service.metrics, SandboxMetrics)
        self.assertIsInstance(service.daemons, SandboxDaemons)
        self.assertIsInstance(service.daemons.heartbeat, SandboxHeartbeatMonitor)
        self.assertIsInstance(service.daemons.heartbeat.policy, SandboxIdlePolicy)
        # The registry is persistence-only: no outward hook — terminal marks
        # run teardown through the lifecycle, the single owner of transitions.
        self.assertFalse(hasattr(service.registry, "on_terminal"))
        # The one inversion left: the lifecycle's provisioning-job probe.
        self.assertIsNotNone(service.lifecycle.job_probe)
        self.assertEqual(service.lifecycle.job_probe, service.provisioner.job_is_live)
        # All collaborators share the one registry (single writer of rows) and
        # the one lifecycle (single owner of transitions).
        self.assertIs(service.lifecycle.registry, service.registry)
        self.assertIs(service.provisioner.registry, service.registry)
        self.assertIs(service.provisioner.lifecycle, service.lifecycle)
        self.assertIs(service.daemons.registry, service.registry)
        self.assertIs(service.daemons.lifecycle, service.lifecycle)
        self.assertIs(service.daemons.heartbeat.registry, service.registry)
        self.assertIs(service.metrics.registry, service.registry)
        self.assertIs(service.worker, self.app.worker)

    def test_lifecycle_is_the_only_writer_of_terminal_marks(self) -> None:
        # Every registry.mark_* call outside the lifecycle would skip teardown
        # (mgmt-key removal + the data-plane teardown task); every direct
        # backend.terminate outside it would skip the liveness confirmation
        # that keeps billing VMs from being stranded behind terminated rows.
        lifecycle_src = (FACADE.parent / "sandbox_lifecycle.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("registry.mark_terminated", lifecycle_src)
        self.assertIn("registry.mark_failed", lifecycle_src)
        for module in (
            "sandboxes.py",
            "commands.py",
            "sandbox_provisioner.py",
            "sandbox_daemons.py",
        ):
            source = (FACADE.parent / module).read_text(encoding="utf-8")
            self.assertNotIn("registry.mark_terminated", source, module)
            self.assertNotIn("registry.mark_failed", source, module)
            self.assertNotIn("backend.terminate", source, module)
            self.assertNotIn(
                "cleanup_orphan(",
                source.replace("lifecycle.cleanup_orphan(", ""),
                module,
            )

    def test_facade_wires_the_task_seam(self) -> None:
        # One channel carries explicit control signals for endpoint refresh and
        # teardown. Unified local mode uses the same neutral control channel as
        # hosted control; conn-file mutation is not in-process anymore.
        service = self.app.sandboxes
        self.assertIsInstance(service.tasks, ControlTaskChannel)
        self.assertIs(service.tasks, service.runtime.lifecycle.tasks)
        self.assertIs(service.store, service.runtime.repository.store)
        self.assertIs(service.backend, service.runtime.lifecycle.backend)
        self.assertIs(service.mgmt_keys, service.runtime.lifecycle.mgmt_keys)
        self.assertIs(service.quotas, self.app.quotas)

    def test_facade_requires_explicit_quota_admission(self) -> None:
        with self.assertRaisesRegex(ValidationError, "quotas is required"):
            SandboxService(
                worker=self.app.worker,
                runtime=self.app.sandbox_runtime,
            )

    def test_facade_requires_quota_admission_port(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "quotas.check_admission is required"
        ):
            SandboxService(
                worker=self.app.worker,
                runtime=self.app.sandbox_runtime,
                quotas=object(),
            )

    def test_facade_requires_lifetime_extension_quota_port(self) -> None:
        class PartialQuota:
            def check_admission(self, **_kwargs):
                return None

        with self.assertRaisesRegex(
            ValidationError, "quotas.check_lifetime_extension is required"
        ):
            SandboxService(
                worker=self.app.worker,
                runtime=self.app.sandbox_runtime,
                quotas=PartialQuota(),
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
        self.assertNotIn("self.metrics.persist_row", source)
        self.assertNotIn("presign_put", source)
        self.assertNotIn("presign_get", source)
        self.assertNotIn("finalize_put", source)
        self.assertNotIn("run_parachute", source)
        self.assertNotIn("PARACHUTE_", source)
        self.assertNotIn("self.parachute", source)
        # All row SQL, including conn-scoped workflow reads, belongs to the
        # repository rather than the stable facade or query handler.
        self.assertNotIn("UPDATE sandboxes", source)
        self.assertNotIn("INSERT INTO sandboxes", source)
        self.assertEqual(source.count("SELECT * FROM sandboxes"), 0)
        query_source = (FACADE.parent / "queries.py").read_text(encoding="utf-8")
        self.assertNotIn("SELECT ", query_source)
        self.assertNotIn(".execute(", query_source)

    def test_facade_delegates_typed_commands_queries_and_maintenance(self) -> None:
        source = FACADE.read_text(encoding="utf-8")
        # Keep the public signatures readable while preventing orchestration
        # policy from accumulating in the stable entry point again.
        self.assertLessEqual(len(source.splitlines()), 300)
        for binding in (
            "self.commands = SandboxCommandHandler(self)",
            "self.queries = SandboxQueryHandler(self)",
            "self.maintenance = SandboxMaintenanceHandler(self)",
            "return self.commands.execute_request(_message(RequestSandboxCommand, locals()))",
            "return self.queries.execute_get(_message(GetSandboxQuery, locals()))",
            "return self.maintenance.reconcile_running_rows()",
        ):
            self.assertIn(binding, source)
        for policy in (
            "validate_request_inputs",
            "read_transcript",
            "release_decision",
            "run_records_view",
            "conn.execute(",
        ):
            self.assertNotIn(policy, source)
        for attribute in (
            "registry",
            "repository",
            "lifecycle",
            "provisioner",
            "metrics",
            "daemons",
            "runs_ledger",
            "transcript_cache",
            "tasks",
        ):
            self.assertIn(f"self.{attribute} =", source)

    def test_repository_owns_workflow_row_reads(self) -> None:
        repository = (FACADE.parent / "sandbox_registry.py").read_text(encoding="utf-8")
        queries = (FACADE.parent / "queries.py").read_text(encoding="utf-8")
        self.assertIn("def rows_for_experiment(", repository)
        self.assertIn("def rows_for_project(", repository)
        self.assertIn("self.repository.rows_for_experiment(", queries)
        self.assertIn("self.repository.rows_for_project(", queries)

    def test_facade_import_does_not_load_proxy_modules(self) -> None:
        code = """
import sys
import merv.brain.sandbox.sandboxes
loaded = sorted(
    name for name in sys.modules
    if name == "merv.proxy" or name.startswith("merv.proxy.")
)
if loaded:
    raise SystemExit("brain import loaded proxy modules: " + ", ".join(loaded))
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(IMPORT_ROOT)
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
