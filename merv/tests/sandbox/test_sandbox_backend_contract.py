from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.sandbox.execution.backends.lambda_labs import (
    sandbox_backend as lambda_backend,
)
from merv.brain.sandbox.execution.backends.modal.sandbox_backend import (
    ModalSandboxBackend,
)
from merv.brain.sandbox.execution.backends.vm_ssh_backend import VmSshSandboxBackend
from merv.brain.sandbox.execution.driver_registry import (
    SANDBOX_DRIVER_REGISTRY,
    sandbox_driver_inventory,
)
from merv.brain.sandbox.execution.multiplexer import MultiplexingSandboxBackend
from merv.brain.sandbox.sandbox_backend import (
    BackendCapabilities,
    ProvisionedSandbox,
    SandboxBackendBase,
    SandboxRequest,
    TranscriptTail,
)
from merv.brain.sandbox.sandbox_daemons import SandboxDaemons
from tests.paths import BACKEND_ROOT, SERVICES_ROOT, SURFACE_ROOT

SANDBOX_ROOT = BACKEND_ROOT / "sandbox"


def _provider_neutral_sandbox_sources():
    return (
        path
        for path in SANDBOX_ROOT.rglob("*.py")
        if "execution" not in path.relative_to(SANDBOX_ROOT).parts
    )


BACKEND_METHODS = (
    "acquire",
    "capabilities_for",
    "is_alive",
    "terminate",
    "read_transcript",
    "sandbox_environment",
    "health",
    "sample_metrics",
    "read_runs",
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
    ) -> TranscriptTail:
        return TranscriptTail(data=b"", total_bytes=0)

    def sandbox_environment(self) -> dict:
        return {"available_tokens": [], "notes": []}

    def health(self) -> dict:
        return {"ok": True}


class SandboxBackendContractTest(unittest.TestCase):
    def _daemons_for_backend(
        self, backend: SandboxBackendBase, *, force_expiry_reaper: bool = False
    ) -> SandboxDaemons:
        return SandboxDaemons(
            registry=object(),  # type: ignore[arg-type]
            backend=backend,
            provisioner=object(),  # type: ignore[arg-type]
            lifecycle=SimpleNamespace(reap_row=lambda **_kwargs: True),  # type: ignore[arg-type]
            sample_metrics=lambda **_kwargs: {},
            force_expiry_reaper=force_expiry_reaper,
        )

    def test_backend_classes_expose_full_contract_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"MERV_MODE": "control"}, clear=False
        ):
            backend_classes = [
                type(
                    SANDBOX_DRIVER_REGISTRY.build(
                        name=descriptor.name, repo_root=Path(tmp)
                    )
                )
                for descriptor in sandbox_driver_inventory()
            ]
        backend_classes.append(MultiplexingSandboxBackend)
        for backend_cls in backend_classes:
            with self.subTest(backend=backend_cls.__name__):
                for method in BACKEND_METHODS:
                    self.assertTrue(
                        callable(getattr(backend_cls, method, None)),
                        f"{backend_cls.__name__}.{method} is missing",
                    )

    def test_base_optional_methods_return_sentinel_defaults(self) -> None:
        backend = MinimalBackend()

        # Single-provider default: one backend serves every request.
        self.assertIs(backend.capabilities_for(provider="anything"), backend.capabilities)
        self.assertIs(backend.management_transport, backend)
        self.assertIsNone(backend.sample_metrics(sandbox_id="sb"))
        self.assertIsNone(backend.read_runs(sandbox_id="sb", workdir="/workspace"))
        self.assertIsNone(backend.refresh_ssh_endpoint(sandbox_id="sb"))
        self.assertIsNone(backend.hardware_catalog())
        self.assertIsNone(backend.find_sandbox_id(experiment_id="exp"))
        self.assertEqual(backend.sandbox_secrets(), {})
        self.assertFalse(
            backend.write_secrets(sandbox_id="sb", secrets={"TOKEN": "value"})
        )
        self.assertIsNone(backend.shutdown())

    def test_vm_and_modal_share_exact_token_environment_default(self) -> None:
        backend = VmSshSandboxBackend()
        note = (
            "HF_TOKEN is available inside the sandbox for Hugging Face downloads. "
            "Do not print or write the token; use it through Hugging Face tooling."
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                backend.sandbox_environment(),
                {"available_tokens": [], "notes": []},
            )
        with mock.patch.dict(os.environ, {"HF_TOKEN": "secret"}, clear=True):
            self.assertEqual(
                backend.sandbox_environment(),
                {"available_tokens": ["HF_TOKEN"], "notes": [note]},
            )

        self.assertIs(
            VmSshSandboxBackend.sandbox_environment,
            SandboxBackendBase.sandbox_environment,
        )
        self.assertIs(
            ModalSandboxBackend.sandbox_environment,
            SandboxBackendBase.sandbox_environment,
        )
        self.assertIs(ModalSandboxBackend.shutdown, SandboxBackendBase.shutdown)
        self.assertIsNot(
            FakeSandboxBackend.sandbox_environment,
            SandboxBackendBase.sandbox_environment,
        )
        self.assertIsNot(
            MultiplexingSandboxBackend.sandbox_environment,
            SandboxBackendBase.sandbox_environment,
        )

    def test_vm_provider_dependencies_are_lazy_cached_and_retry_failures(self) -> None:
        cloud = object()
        config = SimpleNamespace(
            cloud=cloud,
            ssh_user="ubuntu",
            sandbox_data_dir="/workspace/data",
        )
        with mock.patch.object(
            lambda_backend.LambdaSandboxConfig,
            "from_env",
            side_effect=[RuntimeError("missing config"), config],
        ) as config_factory:
            backend = lambda_backend.LambdaLabsSandboxBackend()
            config_factory.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "missing config"):
                _ = backend.config
            self.assertIs(backend.config, config)
            self.assertIs(backend.config, config)
            self.assertEqual(config_factory.call_count, 2)

        built_client = object()
        with mock.patch.object(
            lambda_backend,
            "LambdaCloudClient",
            side_effect=[RuntimeError("client unavailable"), built_client],
        ) as client_factory:
            backend = lambda_backend.LambdaLabsSandboxBackend(config=config)
            client_factory.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "client unavailable"):
                _ = backend.client
            self.assertIs(backend.client, built_client)
            self.assertIs(backend.client, built_client)
            self.assertEqual(
                client_factory.call_args_list,
                [mock.call(config=cloud), mock.call(config=cloud)],
            )

        injected_client = object()
        with mock.patch.object(lambda_backend, "LambdaCloudClient") as client_factory:
            backend = lambda_backend.LambdaLabsSandboxBackend(
                config=config,
                client=injected_client,
            )
            self.assertIs(backend.config, config)
            self.assertIs(backend.client, injected_client)
            client_factory.assert_not_called()

    def test_provisioned_vm_fields_preserve_exact_values_and_order(self) -> None:
        accesses: list[str] = []

        class RecordingConfig:
            @property
            def ssh_user(self) -> str:
                accesses.append("ssh_user")
                return "ubuntu"

            @property
            def sandbox_data_dir(self) -> str:
                accesses.append("sandbox_data_dir")
                return "/workspace/data"

        backend = lambda_backend.LambdaLabsSandboxBackend(
            config=RecordingConfig(),  # type: ignore[arg-type]
            client=object(),  # type: ignore[arg-type]
        )
        fields = backend._provisioned_vm_fields(workdir="/workspace/exp_1")

        self.assertEqual(
            list(fields.items()),
            [
                ("ssh_user", "ubuntu"),
                ("workdir", "/workspace/exp_1"),
                ("volume_name", ""),
                ("sync_dir", "/workspace/exp_1"),
                ("unsynced_dir", "/workspace/data"),
                ("sandbox_data_dir", "/workspace/data"),
                ("reused", False),
            ],
        )
        self.assertEqual(
            accesses,
            ["ssh_user", "sandbox_data_dir", "sandbox_data_dir"],
        )
        self.assertEqual(
            ProvisionedSandbox(
                sandbox_id="vm-1",
                ssh_host="203.0.113.8",
                ssh_port=22,
                **fields,
                gpu="H100",
            ),
            ProvisionedSandbox(
                sandbox_id="vm-1",
                ssh_host="203.0.113.8",
                ssh_port=22,
                ssh_user="ubuntu",
                workdir="/workspace/exp_1",
                volume_name="",
                sync_dir="/workspace/exp_1",
                unsynced_dir="/workspace/data",
                sandbox_data_dir="/workspace/data",
                reused=False,
                gpu="H100",
            ),
        )

    def test_services_do_not_probe_backend_optional_methods(self) -> None:
        for path in (*SERVICES_ROOT.rglob("*.py"), *_provider_neutral_sandbox_sources()):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("getattr(self.backend", source)
                self.assertNotIn("hasattr(self.backend", source)
                self.assertNotIn("getattr(caps", source)

        for path in (
            SURFACE_ROOT / "control" / "control_app.py",
            SURFACE_ROOT / "composition" / "control_mode.py",
        ):
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("getattr(self.execution_backend", source)

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
        # mode. The flag is composition-injected: the control composition root
        # passes force_expiry_reaper=True instead of the daemons reading the
        # process mode from config (module-boundary fix, phase 4a).
        daemons = self._daemons_for_backend(MinimalBackend(), force_expiry_reaper=True)
        with mock.patch.dict(
            os.environ,
            {"RESEARCH_PLUGIN_SANDBOX_REAPER": "0"},
        ):
            self.assertTrue(daemons._reaper_enabled())

    def test_control_composition_forces_the_expiry_reaper(self) -> None:
        # The control composition (not the sandbox module) must compute the
        # force flag — the daemons no longer import merv.brain.surface.config.
        control_source = (SURFACE_ROOT / "composition" / "control_mode.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("force_expiry_reaper=True", control_source)
        daemons_source = (
            BACKEND_ROOT / "sandbox" / "sandbox_daemons.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("resolve_mode", daemons_source)

    def test_services_do_not_dispatch_on_provider_name_literals(self) -> None:
        provider_names = {
            descriptor.name
            for descriptor in sandbox_driver_inventory()
            if not descriptor.test_only
        }
        for path in (*SERVICES_ROOT.rglob("*.py"), *_provider_neutral_sandbox_sources()):
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
