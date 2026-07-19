"""TensorDock backend: dedicated-IP filter, shape encoding, liveness."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from merv.brain.sandbox.execution.backends.tensordock.catalog import (
    parse_instance_type,
    to_agent_options,
)
from merv.brain.sandbox.execution.backends.tensordock.config import (
    TensorDockCloudConfig,
    TensorDockSandboxConfig,
)
from merv.brain.sandbox.execution.backends.tensordock.sandbox_backend import (
    TensorDockSandboxBackend,
)
from merv.brain.sandbox.sandbox_backend import (
    BackendUnavailableError,
    BackendValidationError,
    CapacityUnavailableError,
    SandboxRequest,
)


LOC_DEDICATED = "loc-dedicated-0001"
LOC_PORT_ONLY = "loc-portmapped-0002"

LOCATIONS = [
    {
        "id": LOC_DEDICATED,
        "city": "Austin",
        "country": "United States",
        "gpus": [
            {
                "v0Name": "h100-sxm5-80gb",
                "displayName": "H100 SXM5 80GB",
                "max_count": 8,
                "price_per_hr": 2.2,
                "resources": {"max_vcpus": 128, "max_ram_gb": 300, "max_storage_gb": 1000},
                "pricing": {
                    "per_vcpu_hr": 0.003,
                    "per_gb_ram_hr": 0.002,
                    "per_gb_storage_hr": 0.00005,
                },
                "network_features": {
                    "dedicated_ip_available": True,
                    "port_forwarding_available": True,
                },
            }
        ],
    },
    {
        "id": LOC_PORT_ONLY,
        "city": "Elsewhere",
        "country": "United States",
        "gpus": [
            {
                "v0Name": "geforcertx4090-pcie-24gb",
                "displayName": "RTX 4090 24GB",
                "max_count": 4,
                "price_per_hr": 0.5,
                "resources": {"max_vcpus": 32, "max_ram_gb": 128, "max_storage_gb": 2000},
                "pricing": {
                    "per_vcpu_hr": 0.003,
                    "per_gb_ram_hr": 0.002,
                    "per_gb_storage_hr": 0.00005,
                },
                "network_features": {
                    "dedicated_ip_available": False,  # port-mapped only: unusable
                    "port_forwarding_available": True,
                },
            }
        ],
    },
]


class FakeTensorDockClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.instances: dict[str, dict] = {}
        self.get_calls = 0

    def list_locations(self):
        return LOCATIONS

    def create_instance(self, **kwargs):
        self.created.append(kwargs)
        self.instances["inst-uuid-1"] = {
            "id": "inst-uuid-1",
            "name": kwargs["name"],
            "status": "starting",
            "ipAddress": "",
            "portForwards": [],
        }
        return {"id": "inst-uuid-1", "status": "starting"}

    def get_instance(self, instance_id):
        instance = self.instances.get(str(instance_id))
        if instance is None:
            raise BackendUnavailableError("not found", status=404)
        self.get_calls += 1
        if self.get_calls >= 2 and instance["status"] != "Terminated":
            instance = {
                **instance,
                "status": "running",
                "ipAddress": "198.51.100.44",
                "rateHourly": 2.31,
            }
        return instance

    def list_instances(self):
        return list(self.instances.values())

    def delete_instance(self, instance_id):
        self.deleted.append(str(instance_id))
        self.instances.pop(str(instance_id), None)


def _backend(client: FakeTensorDockClient) -> TensorDockSandboxBackend:
    config = TensorDockSandboxConfig(cloud=TensorDockCloudConfig(token="t"))
    return TensorDockSandboxBackend(config=config, client=client)


def _request(**overrides) -> SandboxRequest:
    fields = {
        "experiment_id": "exp_1",
        "project_id": "proj_1",
        "public_key": "ssh-ed25519 AAAA user",
        "management_public_key": "ssh-ed25519 BBBB mgmt",
        "sandbox_uid": "uid123",
        "instance_type": "1x-h100-sxm5-80gb",
    }
    fields.update(overrides)
    return SandboxRequest(**fields)


class TensorDockAcquireTest(unittest.TestCase):
    def test_acquire_requests_dedicated_ip_and_wraps_bootstrap(self) -> None:
        client = FakeTensorDockClient()
        backend = _backend(client)
        with patch.object(TensorDockSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        self.assertEqual(provisioned.sandbox_id, "inst-uuid-1")
        self.assertEqual(provisioned.ssh_host, "198.51.100.44")
        self.assertEqual(provisioned.ssh_port, 22)
        self.assertEqual(provisioned.ssh_user, "root")
        self.assertEqual(provisioned.region, LOC_DEDICATED)
        self.assertEqual(provisioned.price_usd_per_hour, 2.31)  # live rate wins
        created = client.created[0]
        self.assertEqual(created["gpus"], {"h100-sxm5-80gb": {"count": 1}})
        self.assertEqual(created["location_id"], LOC_DEDICATED)
        self.assertEqual(created["storage_gb"], 100)  # the 100GB minimum
        self.assertEqual(created["ssh_key"], "ssh-ed25519 AAAA user")
        cloud_init = created["cloud_init"]
        self.assertIn("#!/usr/bin/env bash", cloud_init["write_files"][0]["content"])
        self.assertEqual(cloud_init["runcmd"], ["bash /opt/merv/bootstrap.sh"])

    def test_acquire_rejects_malformed_instance_type(self) -> None:
        backend = _backend(FakeTensorDockClient())
        with self.assertRaisesRegex(BackendValidationError, "count.*x"):
            backend.acquire(request=_request(instance_type="h100-sxm5-80gb"))

    def test_acquire_rejects_port_mapped_only_gpus(self) -> None:
        # The 4090 host lacks dedicated IPs, so its shape is never offered.
        backend = _backend(FakeTensorDockClient())
        with self.assertRaisesRegex(BackendValidationError, "not offered"):
            backend.acquire(request=_request(instance_type="1x-geforcertx4090-pcie-24gb"))

    def test_acquire_failure_deletes_the_instance(self) -> None:
        client = FakeTensorDockClient()
        backend = _backend(client)
        with patch.object(
            TensorDockSandboxBackend,
            "_wait_for_running_instance",
            side_effect=BackendUnavailableError("boom"),
        ):
            with self.assertRaises(BackendUnavailableError):
                backend.acquire(request=_request())

        self.assertEqual(client.deleted, ["inst-uuid-1"])


class TensorDockLivenessTest(unittest.TestCase):
    def test_404_is_authoritatively_dead(self) -> None:
        backend = _backend(FakeTensorDockClient())
        self.assertFalse(backend.is_alive(sandbox_id="missing"))

    def test_stopped_still_counts_as_alive_case_insensitively(self) -> None:
        client = FakeTensorDockClient()
        client.instances["i1"] = {"id": "i1", "status": "StoppedDisassociated"}
        client.get_calls = -100
        backend = _backend(client)
        self.assertTrue(backend.is_alive(sandbox_id="i1"))

    def test_terminated_reads_dead(self) -> None:
        client = FakeTensorDockClient()
        client.instances["i1"] = {"id": "i1", "status": "Terminated"}
        backend = _backend(client)
        self.assertFalse(backend.is_alive(sandbox_id="i1"))

    def test_terminate_treats_404_as_already_gone(self) -> None:
        backend = _backend(FakeTensorDockClient())
        self.assertTrue(backend.terminate(sandbox_id="missing"))


class TensorDockCatalogTest(unittest.TestCase):
    def test_options_filter_on_dedicated_ip_capability(self) -> None:
        options = to_agent_options(LOCATIONS)

        names = {o["instance_type"] for o in options}
        self.assertEqual(names, {"1x-h100-sxm5-80gb", "8x-h100-sxm5-80gb"})
        self.assertTrue(all("4090" not in o["gpu_description"] for o in options))

    def test_synthesized_shapes_respect_steps_and_minimums(self) -> None:
        options = to_agent_options(LOCATIONS)
        one_x = next(o for o in options if o["instance_type"] == "1x-h100-sxm5-80gb")
        eight_x = next(o for o in options if o["instance_type"] == "8x-h100-sxm5-80gb")

        self.assertEqual(one_x["vcpus"], 8)
        self.assertEqual(one_x["memory_gib"], 32)
        self.assertEqual(one_x["storage_gib"], 100)
        # 8x clips to the location maxima and the allowed RAM steps.
        self.assertEqual(eight_x["vcpus"], 64)
        self.assertEqual(eight_x["memory_gib"], 256)
        self.assertAlmostEqual(
            one_x["price_usd_per_hour"], 2.2 + 0.003 * 8 + 0.002 * 32 + 0.00005 * 100, places=4
        )

    def test_parse_instance_type_round_trip(self) -> None:
        self.assertEqual(parse_instance_type("8x-h100-sxm5-80gb"), (8, "h100-sxm5-80gb"))
        self.assertIsNone(parse_instance_type("h100"))

    def test_catalog_mentions_prepaid_per_second_billing(self) -> None:
        backend = _backend(FakeTensorDockClient())
        catalog = backend.hardware_catalog()

        self.assertEqual(catalog["provider"], "tensordock")
        self.assertIn("per-second", catalog["reason"])
        self.assertIn("DEDICATED", catalog["reason"])


if __name__ == "__main__":
    unittest.main()
