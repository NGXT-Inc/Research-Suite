"""Static sandbox driver inventory and reusable offline conformance."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from merv.brain.sandbox.execution import build_sandbox_backend
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.sandbox.execution.backends.vm_ssh_backend import VmSshSandboxBackend
from merv.brain.sandbox.execution.driver_registry import (
    DEFAULT_SANDBOX_DRIVER,
    SANDBOX_DRIVER_ALIASES,
    SANDBOX_DRIVER_DESCRIPTORS,
    SandboxDriverDescriptor,
    build_sandbox_driver,
    canonical_sandbox_driver_name,
    sandbox_driver_descriptor,
    sandbox_driver_inventory,
)
from merv.brain.sandbox.sandbox_backend import (
    BackendUnavailableError,
    BackendValidationError,
    SandboxRequest,
)
from tests.paths import BACKEND_ROOT, IMPORT_ROOT
from tests.sandbox.driver_conformance import (
    OfflineDriverFixture,
    assert_catalog_envelope,
    assert_driver_surface,
    exercise_offline_driver,
)


EXPECTED_DRIVERS = {
    "lambda_labs",
    "thunder_compute",
    "modal",
    "hyperstack",
    "digitalocean",
    "verda",
    "voltage_park",
    "tensordock",
    "fake",
}
EXPECTED_ALIASES = {
    "lambda": "lambda_labs",
    "lambdalabs": "lambda_labs",
    "thunder": "thunder_compute",
    "thundercompute": "thunder_compute",
    "datacrunch": "verda",
    "voltagepark": "voltage_park",
}


class SandboxDriverTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_runtime_inventory_is_complete_unique_and_canonical(self) -> None:
        descriptors = sandbox_driver_inventory()
        names = [descriptor.name for descriptor in descriptors]

        self.assertEqual(set(names), EXPECTED_DRIVERS)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(DEFAULT_SANDBOX_DRIVER, "lambda_labs")
        self.assertIs(descriptors, SANDBOX_DRIVER_DESCRIPTORS)
        self.assertEqual(dict(SANDBOX_DRIVER_ALIASES), EXPECTED_ALIASES)
        for descriptor in descriptors:
            with self.subTest(driver=descriptor.name):
                self.assertNotIn(":", descriptor.name)
                self.assertEqual(
                    canonical_sandbox_driver_name(descriptor.name),
                    descriptor.name,
                )
                for alias in descriptor.aliases:
                    self.assertEqual(
                        canonical_sandbox_driver_name(alias), descriptor.name
                    )

    def test_every_registered_factory_builds_compatibility_surface_offline(self) -> None:
        # Constructors remain lazy: no health, catalog, acquire, or cloud call
        # is made here, and control mode disables checkout-adjacent env files.
        with (
            mock.patch.dict(os.environ, {"MERV_MODE": "control"}, clear=True),
            mock.patch(
                "merv.brain.sandbox.execution.backends._http.urlopen",
                side_effect=AssertionError("driver construction attempted network I/O"),
            ),
            mock.patch(
                "socket.create_connection",
                side_effect=AssertionError("driver construction attempted network I/O"),
            ),
        ):
            for descriptor in sandbox_driver_inventory():
                with self.subTest(driver=descriptor.name):
                    backend = build_sandbox_driver(
                        name=descriptor.name,
                        repo_root=self.repo,
                    )
                    assert_driver_surface(
                        self, descriptor=descriptor, backend=backend
                    )

    def test_inventory_and_modal_construction_do_not_import_modal_sdk(self) -> None:
        code = """
import sys
from pathlib import Path
from merv.brain.sandbox.execution.driver_registry import (
    build_sandbox_driver, sandbox_driver_inventory,
)
assert sandbox_driver_inventory()
assert "modal" not in sys.modules
provider_prefix = "merv.brain.sandbox.execution.backends."
assert not any(name.startswith(provider_prefix) for name in sys.modules)
build_sandbox_driver(name="modal", repo_root=Path("/tmp/merv-driver"))
assert "modal" not in sys.modules
assert provider_prefix + "modal" in sys.modules
assert provider_prefix + "lambda_labs" not in sys.modules
"""
        env = {"MERV_MODE": "control", "PYTHONPATH": str(IMPORT_ROOT)}
        subprocess.run([sys.executable, "-c", code], check=True, env=env)

    def test_modal_keeps_its_non_vm_catalog_shape(self) -> None:
        descriptor = sandbox_driver_descriptor("modal")
        with mock.patch.dict(os.environ, {"MERV_MODE": "control"}, clear=True):
            backend = build_sandbox_driver(
                name="modal", repo_root=self.repo
            )

        self.assertNotIsInstance(backend, VmSshSandboxBackend)
        catalog = assert_catalog_envelope(
            self, descriptor=descriptor, backend=backend
        )
        self.assertEqual(catalog["select_with"], "gpu+cpu+memory")
        self.assertIn("gpus", catalog)
        self.assertNotIn("options", catalog)

    def test_descriptors_reject_unsafe_names_and_unknown_drivers(self) -> None:
        with self.assertRaises(BackendValidationError):
            SandboxDriverDescriptor(
                name="bad:name",
                factory_ref="some.module:factory",
            )
        with self.assertRaisesRegex(
            BackendUnavailableError, "unknown execution backend"
        ):
            build_sandbox_driver(name="missing", repo_root=self.repo)

    def test_factory_uses_static_table_instead_of_provider_name_dispatch(self) -> None:
        source = (
            BACKEND_ROOT / "sandbox" / "execution" / "__init__.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('if name == "', source)
        self.assertNotIn("from .backends.", source)

    def test_aliases_still_build_the_canonical_driver(self) -> None:
        with mock.patch.dict(os.environ, {"MERV_MODE": "control"}, clear=True):
            for alias, canonical in EXPECTED_ALIASES.items():
                with self.subTest(alias=alias):
                    backend = build_sandbox_backend(
                        repo_root=self.repo,
                        name=alias,
                    )
                    self.assertEqual(backend.capabilities.name, canonical)


class OfflineDriverConformanceTest(unittest.TestCase):
    def test_shared_harness_covers_lifecycle_catalog_and_management(self) -> None:
        descriptor = sandbox_driver_descriptor("fake")
        backend = FakeSandboxBackend()
        fixture = OfflineDriverFixture(
            descriptor=descriptor,
            backend=backend,
            request=SandboxRequest(
                experiment_id="exp_offline",
                project_id="proj_offline",
                public_key="ssh-ed25519 AAAA offline",
                management_public_key="ssh-ed25519 BBBB management",
            ),
            set_transcript=lambda experiment_id, text: backend.transcripts.__setitem__(
                experiment_id, text
            ),
            set_metrics=lambda sandbox_id, metrics: backend.metrics.__setitem__(
                sandbox_id, metrics
            ),
            set_runs=lambda sandbox_id, listing: backend.run_listings.__setitem__(
                sandbox_id, listing
            ),
            move_endpoint=lambda sandbox_id, host, port: backend.move_endpoint(
                sandbox_id=sandbox_id, host=host, port=port
            ),
            expected_refreshed_endpoint=("moved.sandbox.test", 2222),
            set_outage=lambda unavailable: setattr(
                backend, "liveness_unavailable", unavailable
            ),
        )

        assert_driver_surface(self, descriptor=descriptor, backend=backend)
        exercise_offline_driver(self, fixture)

        selecting = FakeSandboxBackend(requires_hardware_selection=True)
        catalog = assert_catalog_envelope(
            self, descriptor=descriptor, backend=selecting
        )
        self.assertEqual(catalog["select_with"], "instance_type")
        self.assertEqual(catalog["count"], len(catalog["options"]))
        prices = [option["price_usd_per_hour"] for option in catalog["options"]]
        self.assertEqual(prices, sorted(prices))


if __name__ == "__main__":
    unittest.main()
