from __future__ import annotations

import ast
import os
import unittest
from unittest import mock

from backend.execution.backends.fake import FakeSandboxBackend
from backend.execution.backends.lambda_labs import LambdaLabsSandboxBackend
from backend.execution.backends.modal.sandbox_backend import ModalSandboxBackend
from backend.execution.backends.thunder_compute import ThunderComputeSandboxBackend
from backend.sandbox.sandbox_backend import (
    BackendCapabilities,
    ProvisionedSandbox,
    SandboxBackendBase,
    SandboxRequest,
)
from backend.services.sandbox.sandbox_daemons import SandboxDaemons
from tests.paths import BACKEND_ROOT, SERVICES_ROOT


BACKEND_METHODS = (
    "acquire",
    "is_alive",
    "terminate",
    "read_transcript",
    "sandbox_environment",
    "health",
    "sample_metrics",
    "refresh_ssh_endpoint",
    "hardware_catalog",
    "find_sandbox_id",
    "sandbox_secrets",
    "write_secrets",
    "shutdown",
)


class MinimalBackend(SandboxBackendBase):
    capabilities = BackendCapabilities(name="minimal")

    def acquire(
        self,
        *,
        request: SandboxRequest,
        on_phase=None,
        on_created=None,
    ) -> ProvisionedSandbox:
        raise NotImplementedError

    def is_alive(self, *, sandbox_id: str) -> bool:
        return False

    def terminate(self, *, sandbox_id: str) -> bool:
        return False

    def read_transcript(
        self,
        *,
        sandbox_id: str,
        experiment_id: str,
        volume_name: str,
        workdir: str,
        tail: int | None = None,
        ssh_host: str = "",
        ssh_port: int = 0,
        ssh_user: str = "",
        key_path: str = "",
    ) -> str:
        return ""

    def sandbox_environment(self) -> dict:
        return {"available_tokens": [], "notes": []}

    def health(self) -> dict:
        return {"ok": True}


class SandboxBackendContractTest(unittest.TestCase):
    def _daemons_for_backend(self, backend: SandboxBackendBase) -> SandboxDaemons:
        return SandboxDaemons(
            registry=object(),  # type: ignore[arg-type]
            backend=backend,
            provisioner=object(),  # type: ignore[arg-type]
            sample_metrics=lambda **_kwargs: {},
        )

    def test_backend_classes_expose_full_contract_surface(self) -> None:
        for backend_cls in (
            ModalSandboxBackend,
            LambdaLabsSandboxBackend,
            ThunderComputeSandboxBackend,
            FakeSandboxBackend,
        ):
            with self.subTest(backend=backend_cls.__name__):
                for method in BACKEND_METHODS:
                    self.assertTrue(
                        callable(getattr(backend_cls, method, None)),
                        f"{backend_cls.__name__}.{method} is missing",
                    )

    def test_base_optional_methods_return_sentinel_defaults(self) -> None:
        backend = MinimalBackend()

        self.assertIsNone(backend.sample_metrics(sandbox_id="sb"))
        self.assertIsNone(backend.refresh_ssh_endpoint(sandbox_id="sb"))
        self.assertIsNone(backend.hardware_catalog())
        self.assertIsNone(backend.find_sandbox_id(experiment_id="exp"))
        self.assertIsNone(backend.shutdown())

    def test_services_do_not_probe_backend_optional_methods(self) -> None:
        for path in SERVICES_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("getattr(self.backend", source)
                self.assertNotIn("hasattr(self.backend", source)
                self.assertNotIn("getattr(caps", source)

        app_source = (BACKEND_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("getattr(self.execution_backend", app_source)

    def test_fake_backend_uses_base_catalog_default_until_selection_enabled(self) -> None:
        plain = FakeSandboxBackend()
        self.assertIsNone(plain.hardware_catalog())

        selecting = FakeSandboxBackend(requires_hardware_selection=True)
        catalog = selecting.hardware_catalog()
        self.assertIsInstance(catalog, dict)
        self.assertTrue(catalog["selection_required"])

    def test_default_capabilities_enable_reaper_gate(self) -> None:
        daemons = self._daemons_for_backend(MinimalBackend())

        with mock.patch.dict(
            os.environ,
            {"RESEARCH_PLUGIN_SANDBOX_REAPER": "1"},
        ):
            self.assertTrue(daemons._reaper_enabled())

    def test_fake_capabilities_disable_reaper_gate(self) -> None:
        daemons = self._daemons_for_backend(FakeSandboxBackend())

        with mock.patch.dict(
            os.environ,
            {"RESEARCH_PLUGIN_SANDBOX_REAPER": "1"},
        ):
            self.assertFalse(daemons._reaper_enabled())

    def test_local_mode_honors_reaper_off_switch(self) -> None:
        # Local mode (the default): the user owns their bill, so the env
        # off-switch disables the reaper even on a backend that enforces expiry.
        daemons = self._daemons_for_backend(MinimalBackend())
        with mock.patch.dict(os.environ, {"RESEARCH_PLUGIN_SANDBOX_REAPER": "0"}):
            self.assertFalse(daemons._reaper_enabled())

    def test_control_mode_ignores_reaper_off_switch(self) -> None:
        # Cost governance (cloud plan Phase 7): the cloud pays for every VM, so
        # an operator-set RESEARCH_PLUGIN_SANDBOX_REAPER=0 is IGNORED in control
        # mode — the reaper stays on (still gated by the backend's enforce_expiry).
        daemons = self._daemons_for_backend(MinimalBackend())
        with mock.patch.dict(
            os.environ,
            {
                "RESEARCH_PLUGIN_MODE": "control",
                "RESEARCH_PLUGIN_SANDBOX_REAPER": "0",
            },
        ):
            self.assertTrue(daemons._reaper_enabled())

    def test_services_do_not_dispatch_on_provider_name_literals(self) -> None:
        provider_names = {"modal", "lambda_labs", "thunder_compute"}
        for path in SERVICES_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            string_literals = {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            with self.subTest(path=path.name):
                self.assertTrue(provider_names.isdisjoint(string_literals))


if __name__ == "__main__":
    unittest.main()
