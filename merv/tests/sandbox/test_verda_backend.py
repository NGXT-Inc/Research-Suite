"""Verda backend: token refresh, deploy resources, liveness, catalog."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.execution.backends.verda.catalog import to_agent_options
from backend.execution.backends.verda.config import (
    VerdaCloudConfig,
    VerdaSandboxConfig,
)
from backend.execution.backends.verda.sandbox_backend import VerdaSandboxBackend
from backend.sandbox.sandbox_backend import (
    BackendUnavailableError,
    BackendValidationError,
    CapacityUnavailableError,
    SandboxRequest,
)


INSTANCE_TYPES = [
    {
        "instance_type": "1H100.80S.30V",
        "model": "H100",
        "gpu": {"description": "1x H100 SXM5 80GB", "number_of_gpus": 1},
        "cpu": {"description": "30 CPU", "number_of_cores": 30},
        "memory": {"description": "120GB RAM", "size_in_gigabytes": 120},
        "price_per_hour": "2.44",
    },
    {
        "instance_type": "8A100.176V",
        "model": "A100",
        "gpu": {"description": "8x A100 80GB", "number_of_gpus": 8},
        "cpu": {"description": "176 CPU", "number_of_cores": 176},
        "memory": {"description": "960GB RAM", "size_in_gigabytes": 960},
        "price_per_hour": "8.80",
    },
]
AVAILABILITY = [
    {"location_code": "FIN-01", "availabilities": ["1H100.80S.30V"]},
    {"location_code": "ICE-01", "availabilities": ["1H100.80S.30V"]},
]


class FakeVerdaClient:
    def __init__(self) -> None:
        self.keys_added: list[dict] = []
        self.keys_deleted: list[str] = []
        self.scripts_added: list[dict] = []
        self.scripts_deleted: list[str] = []
        self.deploys: list[dict] = []
        self.actions: list[dict] = []
        self.instances: dict[str, dict] = {}
        self.get_calls = 0

    def list_instance_types(self):
        return INSTANCE_TYPES

    def list_availability(self):
        return AVAILABILITY

    def add_ssh_key(self, *, name, key):
        self.keys_added.append({"name": name, "key": key})
        return "key-uuid-1"

    def list_ssh_keys(self):
        return [{"id": "key-uuid-1", "name": k["name"]} for k in self.keys_added]

    def delete_ssh_key(self, key_id):
        self.keys_deleted.append(key_id)

    def add_script(self, *, name, script):
        self.scripts_added.append({"name": name, "script": script})
        return "script-uuid-1"

    def list_scripts(self):
        return [{"id": "script-uuid-1", "name": s["name"]} for s in self.scripts_added]

    def delete_script(self, script_id):
        self.scripts_deleted.append(script_id)

    def deploy_instance(self, **kwargs):
        self.deploys.append(kwargs)
        self.instances["inst-uuid-1"] = {
            "id": "inst-uuid-1",
            "hostname": kwargs["hostname"],
            "status": "provisioning",
            "ip": "",
        }
        return "inst-uuid-1"

    def get_instance(self, instance_id):
        instance = self.instances.get(str(instance_id))
        if instance is None:
            raise BackendUnavailableError("not found", status=404)
        self.get_calls += 1
        if self.get_calls >= 2 and instance["status"] == "provisioning":
            instance = {
                **instance,
                "status": "running",
                "ip": "203.0.113.77",
                "price_per_hour": 2.31,
            }
        return instance

    def list_instances(self):
        return list(self.instances.values())

    def perform_action(self, *, instance_id, action):
        self.actions.append({"id": instance_id, "action": action})
        if action == "delete":
            self.instances.pop(str(instance_id), None)


def _backend(client: FakeVerdaClient) -> VerdaSandboxBackend:
    config = VerdaSandboxConfig(
        cloud=VerdaCloudConfig(client_id="c", client_secret="s")
    )
    return VerdaSandboxBackend(config=config, client=client)


def _request(**overrides) -> SandboxRequest:
    fields = {
        "experiment_id": "exp_1",
        "project_id": "proj_1",
        "public_key": "ssh-ed25519 AAAA test",
        "sandbox_uid": "uid123",
        "instance_type": "1H100.80S.30V",
    }
    fields.update(overrides)
    return SandboxRequest(**fields)


class VerdaAcquireTest(unittest.TestCase):
    def test_acquire_registers_key_and_script_then_deploys(self) -> None:
        client = FakeVerdaClient()
        backend = _backend(client)
        with patch.object(VerdaSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        self.assertEqual(provisioned.sandbox_id, "inst-uuid-1")
        self.assertEqual(provisioned.ssh_host, "203.0.113.77")
        self.assertEqual(provisioned.ssh_user, "root")
        self.assertEqual(provisioned.region, "FIN-01")  # deterministic first
        # Live per-instance quote beats the catalog price.
        self.assertEqual(provisioned.price_usd_per_hour, 2.31)
        deploy = client.deploys[0]
        self.assertEqual(deploy["ssh_key_ids"], ["key-uuid-1"])
        self.assertEqual(deploy["startup_script_id"], "script-uuid-1")
        self.assertEqual(deploy["image"], "ubuntu-24.04")
        self.assertIn("#!/usr/bin/env bash", client.scripts_added[0]["script"])

    def test_acquire_no_capacity_anywhere_raises_capacity_error(self) -> None:
        backend = _backend(FakeVerdaClient())
        with self.assertRaises(CapacityUnavailableError):
            backend.acquire(request=_request(instance_type="8A100.176V"))

    def test_acquire_unknown_type_is_a_validation_error(self) -> None:
        backend = _backend(FakeVerdaClient())
        with self.assertRaisesRegex(BackendValidationError, "not offered"):
            backend.acquire(request=_request(instance_type="9B200"))

    def test_acquire_failure_deletes_instance_script_and_key(self) -> None:
        client = FakeVerdaClient()
        backend = _backend(client)
        with patch.object(
            VerdaSandboxBackend,
            "_wait_for_running_instance",
            side_effect=BackendUnavailableError("boom"),
        ):
            with self.assertRaises(BackendUnavailableError):
                backend.acquire(request=_request())

        self.assertEqual(client.actions, [{"id": "inst-uuid-1", "action": "delete"}])
        self.assertEqual(client.scripts_deleted, ["script-uuid-1"])
        self.assertEqual(client.keys_deleted, ["key-uuid-1"])

    def test_no_capacity_status_during_wait_raises_capacity_error(self) -> None:
        client = FakeVerdaClient()

        def deploy(**kwargs):
            client.deploys.append(kwargs)
            client.instances["inst-uuid-1"] = {
                "id": "inst-uuid-1",
                "hostname": kwargs["hostname"],
                "status": "no_capacity",
                "ip": "",
            }
            return "inst-uuid-1"

        client.deploy_instance = deploy
        backend = _backend(client)
        with self.assertRaises(CapacityUnavailableError):
            backend.acquire(request=_request())


class VerdaLivenessTest(unittest.TestCase):
    def test_404_is_authoritatively_dead(self) -> None:
        backend = _backend(FakeVerdaClient())
        self.assertFalse(backend.is_alive(sandbox_id="missing"))

    def test_offline_still_counts_as_alive(self) -> None:
        client = FakeVerdaClient()
        client.instances["i1"] = {"id": "i1", "status": "offline", "ip": ""}
        client.get_calls = -100
        backend = _backend(client)
        self.assertTrue(backend.is_alive(sandbox_id="i1"))

    def test_terminate_uses_delete_action_and_drops_rp_resources(self) -> None:
        client = FakeVerdaClient()
        backend = _backend(client)
        with patch.object(VerdaSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        self.assertTrue(backend.terminate(sandbox_id=provisioned.sandbox_id))
        self.assertIn(
            {"id": "inst-uuid-1", "action": "delete"}, client.actions
        )


class VerdaCatalogTest(unittest.TestCase):
    def test_options_join_availability_and_parse_string_prices(self) -> None:
        options = to_agent_options(INSTANCE_TYPES, AVAILABILITY)

        self.assertEqual([o["instance_type"] for o in options], ["1H100.80S.30V"])
        option = options[0]
        self.assertEqual(option["price_usd_per_hour"], 2.44)
        self.assertEqual(option["regions"], ["FIN-01", "ICE-01"])
        self.assertEqual(option["vcpus"], 30)

    def test_catalog_mentions_ten_minute_billing(self) -> None:
        backend = _backend(FakeVerdaClient())
        catalog = backend.hardware_catalog()

        self.assertEqual(catalog["provider"], "verda")
        self.assertIn("10-minute", catalog["reason"])


if __name__ == "__main__":
    unittest.main()
