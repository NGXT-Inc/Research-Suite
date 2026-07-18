"""Hyperstack backend: acquire flow, liveness semantics, catalog join."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.execution.backends.hyperstack.catalog import to_agent_options
from backend.execution.backends.hyperstack.config import (
    HyperstackCloudConfig,
    HyperstackSandboxConfig,
)
from backend.execution.backends.hyperstack.sandbox_backend import (
    SSH_INGRESS_RULES,
    HyperstackSandboxBackend,
)
from backend.sandbox.sandbox_backend import (
    BackendUnavailableError,
    BackendValidationError,
    CapacityUnavailableError,
    SandboxRequest,
)


FLAVOR_GROUPS = [
    {
        "gpu": "A100-80G-PCIe",
        "region_name": "CANADA-1",
        "flavors": [
            {
                "id": 1,
                "name": "n3-A100x1",
                "region_name": "CANADA-1",
                "cpu": 28,
                "ram": 120,
                "disk": 100,
                "ephemeral": 750,
                "gpu": "A100-80G-PCIe",
                "gpu_count": 1,
                "stock_available": True,
            },
            {
                "id": 2,
                "name": "n3-A100x2",
                "region_name": "CANADA-1",
                "cpu": 56,
                "ram": 240,
                "disk": 100,
                "ephemeral": 1500,
                "gpu": "A100-80G-PCIe",
                "gpu_count": 2,
                "stock_available": False,
            },
        ],
    }
]
PRICEBOOK = [
    {"name": "n3-A100x1", "value": "1.35"},
    {"name": "n3-A100x2", "value": "2.70"},
]


class FakeHyperstackClient:
    def __init__(self) -> None:
        self.keypairs_imported: list[dict] = []
        self.keypairs_deleted: list = []
        self.vms_created: list[dict] = []
        self.vms_deleted: list[str] = []
        self.vm_states: dict[str, dict] = {}
        self.get_calls = 0

    def list_flavors(self, *, region=None):
        return FLAVOR_GROUPS

    def get_pricebook(self):
        return PRICEBOOK

    def import_keypair(self, **kwargs):
        self.keypairs_imported.append(kwargs)
        return {"id": 4501, "name": kwargs["name"]}

    def list_keypairs(self):
        return [
            {"id": 4501, "name": imported["name"]}
            for imported in self.keypairs_imported
        ]

    def delete_keypair(self, keypair_id):
        self.keypairs_deleted.append(keypair_id)

    def create_vm(self, **kwargs):
        self.vms_created.append(kwargs)
        self.vm_states["1402"] = {
            "id": 1402,
            "name": kwargs["name"],
            "status": "CREATING",
            "floating_ip": None,
            "keypair": {"name": kwargs["key_name"]},
        }
        return {"id": 1402, "name": kwargs["name"], "status": "CREATING"}

    def get_vm(self, vm_id):
        state = self.vm_states.get(str(vm_id))
        if state is None:
            raise BackendUnavailableError("not found", status=404)
        self.get_calls += 1
        if self.get_calls >= 2:
            state = {
                **state,
                "status": "ACTIVE",
                "floating_ip": "203.0.113.5",
                "environment": {"region": "CANADA-1"},
                "flavor": {"name": "n3-A100x1", "cpu": 28, "ram": 120, "gpu": "A100-80G-PCIe"},
            }
        return state

    def list_vms(self):
        return list(self.vm_states.values())

    def delete_vm(self, vm_id):
        self.vms_deleted.append(str(vm_id))
        self.vm_states.pop(str(vm_id), None)


def _backend(client: FakeHyperstackClient) -> HyperstackSandboxBackend:
    config = HyperstackSandboxConfig(
        cloud=HyperstackCloudConfig(api_key="k"),
        environment_name="test-env",
    )
    return HyperstackSandboxBackend(config=config, client=client)


def _request(**overrides) -> SandboxRequest:
    fields = {
        "experiment_id": "exp_1",
        "project_id": "proj_1",
        "public_key": "ssh-ed25519 AAAA test",
        "sandbox_uid": "uid123",
        "instance_type": "n3-A100x1",
    }
    fields.update(overrides)
    return SandboxRequest(**fields)


class HyperstackAcquireTest(unittest.TestCase):
    def test_acquire_opens_ssh_ingress_and_returns_connection_facts(self) -> None:
        client = FakeHyperstackClient()
        backend = _backend(client)
        backend.config  # materialize
        with patch.object(HyperstackSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        self.assertEqual(provisioned.sandbox_id, "1402")
        self.assertEqual(provisioned.ssh_host, "203.0.113.5")
        self.assertEqual(provisioned.ssh_user, "ubuntu")
        self.assertEqual(provisioned.instance_type, "n3-A100x1")
        self.assertEqual(provisioned.region, "CANADA-1")
        self.assertEqual(provisioned.price_usd_per_hour, 1.35)
        created = client.vms_created[0]
        # Secure-by-default: creation must open TCP 22 or SSH never works.
        self.assertEqual(created["security_rules"], SSH_INGRESS_RULES)
        self.assertEqual(created["environment_name"], "test-env")
        self.assertIn("#!/usr/bin/env bash", created["user_data"])
        self.assertEqual(client.keypairs_imported[0]["public_key"], "ssh-ed25519 AAAA test")

    def test_acquire_requires_instance_type(self) -> None:
        backend = _backend(FakeHyperstackClient())
        with self.assertRaisesRegex(BackendValidationError, "instance_type"):
            backend.acquire(request=_request(instance_type=None))

    def test_acquire_no_stock_raises_capacity_error(self) -> None:
        backend = _backend(FakeHyperstackClient())
        with self.assertRaises(CapacityUnavailableError):
            backend.acquire(request=_request(instance_type="n3-A100x2"))

    def test_acquire_failure_cleans_up_vm_and_keypair(self) -> None:
        client = FakeHyperstackClient()
        backend = _backend(client)
        with patch.object(
            HyperstackSandboxBackend,
            "_wait_for_active_vm",
            side_effect=BackendUnavailableError("boom"),
        ):
            with self.assertRaises(BackendUnavailableError):
                backend.acquire(request=_request())

        self.assertEqual(client.vms_deleted, ["1402"])
        self.assertEqual([str(k) for k in client.keypairs_deleted], ["4501"])


class HyperstackLivenessTest(unittest.TestCase):
    def test_404_is_authoritatively_dead(self) -> None:
        backend = _backend(FakeHyperstackClient())
        self.assertFalse(backend.is_alive(sandbox_id="99999"))

    def test_shutoff_still_counts_as_alive(self) -> None:
        # SHUTOFF bills; reading it as dead would strand a billing VM.
        client = FakeHyperstackClient()
        client.vm_states["7"] = {"id": 7, "status": "SHUTOFF"}
        client.get_calls = 99  # skip the CREATING->ACTIVE ramp
        backend = _backend(client)
        client.vm_states["7"]["status"] = "SHUTOFF"
        self.assertTrue(backend.is_alive(sandbox_id="7"))

    def test_outage_raises_rather_than_reporting_dead(self) -> None:
        class OutageClient(FakeHyperstackClient):
            def get_vm(self, vm_id):
                raise BackendUnavailableError("gateway timeout", status=504)

        backend = _backend(OutageClient())
        with self.assertRaises(BackendUnavailableError):
            backend.is_alive(sandbox_id="7")

    def test_terminate_deletes_vm_and_rp_keypair(self) -> None:
        client = FakeHyperstackClient()
        backend = _backend(client)
        with patch.object(HyperstackSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        self.assertTrue(backend.terminate(sandbox_id=provisioned.sandbox_id))
        self.assertIn("1402", client.vms_deleted)
        self.assertEqual(client.keypairs_deleted, [4501])


class HyperstackCatalogTest(unittest.TestCase):
    def test_options_join_pricebook_and_filter_stock(self) -> None:
        options = to_agent_options(FLAVOR_GROUPS, PRICEBOOK)

        self.assertEqual([o["instance_type"] for o in options], ["n3-A100x1"])
        option = options[0]
        self.assertEqual(option["price_usd_per_hour"], 1.35)
        self.assertEqual(option["gpu"], "A100")
        self.assertEqual(option["vcpus"], 28)
        self.assertEqual(option["memory_gib"], 120)
        self.assertEqual(option["storage_gib"], 850)
        self.assertEqual(option["regions"], ["CANADA-1"])

    def test_hardware_catalog_shape(self) -> None:
        backend = _backend(FakeHyperstackClient())
        catalog = backend.hardware_catalog()

        self.assertEqual(catalog["provider"], "hyperstack")
        self.assertTrue(catalog["selection_required"])
        self.assertEqual(catalog["regions"], ["CANADA-1"])
        self.assertEqual(catalog["count"], 1)


if __name__ == "__main__":
    unittest.main()
