"""Voltage Park backend: instant deploy, cloud-init wrap, liveness, presets."""

from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from merv.brain.sandbox.execution.backends.voltage_park.catalog import to_agent_options
from merv.brain.sandbox.execution.backends.voltage_park.config import (
    VoltageParkCloudConfig,
    VoltageParkSandboxConfig,
)
from merv.brain.sandbox.execution.backends.voltage_park.sandbox_backend import (
    VoltageParkSandboxBackend,
)
from merv.brain.sandbox.execution.driver_registry import sandbox_driver_descriptor
from merv.brain.sandbox.sandbox_backend import (
    BackendUnavailableError,
    BackendValidationError,
    CapacityUnavailableError,
    SandboxRequest,
)
from tests.sandbox.driver_conformance import (
    assert_catalog_envelope,
    assert_driver_surface,
)


PRESET_1X = "11111111-1111-1111-1111-111111111111"
PRESET_8X = "88888888-8888-8888-8888-888888888888"
PRESET_WIN = "99999999-9999-9999-9999-999999999999"
LOCATION = "aaaaaaaa-0000-0000-0000-000000000001"

LOCATIONS = [
    {
        "id": LOCATION,
        "available_presets": [
            {
                "id": PRESET_1X,
                "resources": {
                    "gpus": {"h100-sxm5-80gb": {"count": 1}},
                    "vcpu_count": 16,
                    "ram_gb": 128,
                    "storage_gb": 500,
                },
                "operating_system": "Ubuntu 22.04 LTS",
                "compute_rate_hourly": "2.500000",
                "storage_rate_hourly": "0.050000",
                "available_vms": 3,
            },
            {
                "id": PRESET_8X,
                "resources": {
                    "gpus": {"h100-sxm5-80gb": {"count": 8}},
                    "vcpu_count": 128,
                    "ram_gb": 1024,
                    "storage_gb": 4000,
                },
                "operating_system": "Ubuntu 22.04 LTS",
                "compute_rate_hourly": "20.000000",
                "storage_rate_hourly": "0.400000",
                "available_vms": 0,
            },
            {
                "id": PRESET_WIN,
                "resources": {
                    "gpus": {"h100-sxm5-80gb": {"count": 1}},
                    "vcpu_count": 16,
                    "ram_gb": 128,
                    "storage_gb": 500,
                },
                "operating_system": "Windows 10",
                "compute_rate_hourly": "2.500000",
                "storage_rate_hourly": "0.050000",
                "available_vms": 2,
            },
        ],
    }
]


class FakeVoltageParkClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.vms: dict[str, dict] = {}
        self.get_calls = 0
        self.port_forward_22 = False

    def list_instant_locations(self):
        return LOCATIONS

    def create_instant_vm(self, **kwargs):
        self.created.append(kwargs)
        self.vms["vm-uuid-1"] = {
            "id": "vm-uuid-1",
            "name": kwargs["name"],
            "status": "Relocating",
            "public_ip": "",
            "port_forwards": [],
        }
        return "vm-uuid-1"

    def get_vm(self, vm_id):
        vm = self.vms.get(str(vm_id))
        if vm is None:
            raise BackendUnavailableError("not found", status=404)
        self.get_calls += 1
        if self.get_calls >= 2 and vm["status"] not in ("Terminated",):
            vm = {
                **vm,
                "status": "Running",
                "public_ip": "203.0.113.99",
                "port_forwards": (
                    [{"internal_port": 22, "external_port": 20040}]
                    if self.port_forward_22
                    else []
                ),
                "pricing": {"total_associated_per_hr": "2.550000"},
            }
        return vm

    def list_vms(self):
        return list(self.vms.values())

    def delete_vm(self, vm_id):
        self.deleted.append(str(vm_id))
        self.vms.pop(str(vm_id), None)


def _backend(client: FakeVoltageParkClient) -> VoltageParkSandboxBackend:
    config = VoltageParkSandboxConfig(cloud=VoltageParkCloudConfig(token="t"))
    return VoltageParkSandboxBackend(config=config, client=client)


def _request(**overrides) -> SandboxRequest:
    fields = {
        "experiment_id": "exp_1",
        "project_id": "proj_1",
        "public_key": "ssh-ed25519 AAAA user",
        "management_public_key": "ssh-ed25519 BBBB mgmt",
        "sandbox_uid": "uid123",
        "instance_type": PRESET_1X,
    }
    fields.update(overrides)
    return SandboxRequest(**fields)


class VoltageParkAcquireTest(unittest.TestCase):
    def test_acquire_sends_keys_and_b64_bootstrap_cloud_init(self) -> None:
        client = FakeVoltageParkClient()
        backend = _backend(client)
        with patch.object(VoltageParkSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        self.assertEqual(provisioned.sandbox_id, "vm-uuid-1")
        self.assertEqual(provisioned.ssh_host, "203.0.113.99")
        self.assertEqual(provisioned.ssh_port, 22)
        self.assertEqual(provisioned.gpu, "H100")
        # Live pricing beats the preset estimate.
        self.assertEqual(provisioned.price_usd_per_hour, 2.55)
        created = client.created[0]
        self.assertEqual(
            created["ssh_keys"], ["ssh-ed25519 AAAA user", "ssh-ed25519 BBBB mgmt"]
        )
        write_file = created["cloud_init"]["write_files"][0]
        self.assertEqual(write_file["encoding"], "b64")
        decoded = base64.b64decode(write_file["content"]).decode("utf-8")
        self.assertIn("#!/usr/bin/env bash", decoded)
        self.assertEqual(created["cloud_init"]["runcmd"], ["bash /opt/merv/bootstrap.sh"])

    def test_acquire_uses_port_forward_when_internal_22_is_mapped(self) -> None:
        client = FakeVoltageParkClient()
        client.port_forward_22 = True
        backend = _backend(client)
        with patch.object(VoltageParkSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        self.assertEqual(provisioned.ssh_port, 20040)

    def test_acquire_sold_out_preset_raises_capacity_error(self) -> None:
        backend = _backend(FakeVoltageParkClient())
        with self.assertRaises(CapacityUnavailableError):
            backend.acquire(request=_request(instance_type=PRESET_8X))

    def test_acquire_unknown_preset_is_a_validation_error(self) -> None:
        backend = _backend(FakeVoltageParkClient())
        with self.assertRaisesRegex(BackendValidationError, "not offered"):
            backend.acquire(request=_request(instance_type="not-a-preset"))

    def test_acquire_failure_deletes_the_vm(self) -> None:
        client = FakeVoltageParkClient()
        backend = _backend(client)
        with patch.object(
            VoltageParkSandboxBackend,
            "_wait_for_running_vm",
            side_effect=BackendUnavailableError("boom"),
        ):
            with self.assertRaises(BackendUnavailableError):
                backend.acquire(request=_request())

        self.assertEqual(client.deleted, ["vm-uuid-1"])


class VoltageParkLivenessTest(unittest.TestCase):
    def test_404_is_authoritatively_dead(self) -> None:
        backend = _backend(FakeVoltageParkClient())
        self.assertFalse(backend.is_alive(sandbox_id="missing"))

    def test_stopped_still_counts_as_alive(self) -> None:
        client = FakeVoltageParkClient()
        client.vms["v1"] = {"id": "v1", "status": "StoppedDisassociated"}
        client.get_calls = -100
        backend = _backend(client)
        self.assertTrue(backend.is_alive(sandbox_id="v1"))

    def test_terminated_status_reads_dead(self) -> None:
        client = FakeVoltageParkClient()
        client.vms["v1"] = {"id": "v1", "status": "Terminated"}
        backend = _backend(client)
        self.assertFalse(backend.is_alive(sandbox_id="v1"))

    def test_terminate_treats_404_as_already_gone(self) -> None:
        backend = _backend(FakeVoltageParkClient())
        self.assertTrue(backend.terminate(sandbox_id="missing"))


class VoltageParkCatalogTest(unittest.TestCase):
    def test_shared_driver_contract_with_injected_client(self) -> None:
        backend = _backend(FakeVoltageParkClient())
        descriptor = sandbox_driver_descriptor("voltage_park")

        assert_driver_surface(self, descriptor=descriptor, backend=backend)
        assert_catalog_envelope(self, descriptor=descriptor, backend=backend)

    def test_options_exclude_windows_and_sold_out_presets(self) -> None:
        options = to_agent_options(LOCATIONS)

        self.assertEqual([o["instance_type"] for o in options], [PRESET_1X])
        option = options[0]
        self.assertEqual(option["gpu"], "H100")
        self.assertEqual(option["gpu_count"], 1)
        self.assertEqual(option["price_usd_per_hour"], 2.55)
        self.assertEqual(option["regions"], [LOCATION])

    def test_hardware_catalog_shape(self) -> None:
        backend = _backend(FakeVoltageParkClient())
        catalog = backend.hardware_catalog()

        self.assertEqual(catalog["provider"], "voltage_park")
        self.assertTrue(catalog["selection_required"])
        self.assertEqual(catalog["count"], 1)


if __name__ == "__main__":
    unittest.main()
