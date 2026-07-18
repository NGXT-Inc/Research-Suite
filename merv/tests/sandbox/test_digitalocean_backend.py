"""DigitalOcean backend: acquire flow, key dedupe, liveness, GPU catalog."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.execution.backends.digitalocean.catalog import to_agent_options
from backend.execution.backends.digitalocean.config import (
    DigitalOceanCloudConfig,
    DigitalOceanSandboxConfig,
)
from backend.execution.backends.digitalocean.sandbox_backend import (
    DigitalOceanSandboxBackend,
)
from backend.sandbox.sandbox_backend import (
    BackendUnavailableError,
    BackendValidationError,
    CapacityUnavailableError,
    SandboxRequest,
)


SIZES = [
    {
        "slug": "s-1vcpu-1gb",
        "vcpus": 1,
        "memory": 1024,
        "disk": 25,
        "price_hourly": 0.00893,
        "regions": ["nyc1"],
        "available": True,
        "description": "Basic",
        # no gpu_info: CPU size, excluded from the menu
    },
    {
        "slug": "gpu-h100x1-80gb",
        "vcpus": 20,
        "memory": 245760,
        "disk": 720,
        "price_hourly": 3.39,
        "regions": ["nyc2", "tor1"],
        "available": True,
        "description": "H100 GPU - 1X",
        "gpu_info": {"count": 1, "model": "nvidia_h100", "vram": {"amount": 80, "unit": "gib"}},
    },
    {
        "slug": "gpu-h100x8-640gb",
        "vcpus": 160,
        "memory": 1966080,
        "disk": 2046,
        "price_hourly": 23.92,
        "regions": [],
        "available": False,
        "description": "H100 GPU - 8X",
        "gpu_info": {"count": 8, "model": "nvidia_h100", "vram": {"amount": 640, "unit": "gib"}},
    },
]


class FakeDigitalOceanClient:
    def __init__(self) -> None:
        self.keys_created: list[dict] = []
        self.keys_deleted: list = []
        self.droplets_created: list[dict] = []
        self.droplets_deleted: list[str] = []
        self.droplets: dict[str, dict] = {}
        self.get_calls = 0
        self.duplicate_key = False

    def list_sizes(self):
        return SIZES

    def create_ssh_key(self, *, name, public_key):
        if self.duplicate_key:
            raise BackendUnavailableError("SSH Key is already in use", status=422)
        self.keys_created.append({"name": name, "public_key": public_key})
        return {"id": 512190, "name": name, "public_key": public_key}

    def list_ssh_keys(self):
        stored = [{"id": 512190, **k} for k in self.keys_created]
        if self.duplicate_key:
            stored.append(
                {"id": 999, "name": "pre-existing", "public_key": "ssh-ed25519 AAAA old"}
            )
        return stored

    def delete_ssh_key(self, key_id):
        self.keys_deleted.append(key_id)

    def create_droplet(self, **kwargs):
        self.droplets_created.append(kwargs)
        droplet = {"id": 3164444, "name": kwargs["name"], "status": "new", "networks": {"v4": []}}
        self.droplets["3164444"] = droplet
        return droplet

    def get_droplet(self, droplet_id):
        droplet = self.droplets.get(str(droplet_id))
        if droplet is None:
            raise BackendUnavailableError("not found", status=404)
        self.get_calls += 1
        if self.get_calls >= 2:
            droplet = {
                **droplet,
                "status": "active",
                "networks": {
                    "v4": [
                        {"ip_address": "10.128.0.2", "type": "private"},
                        {"ip_address": "104.236.32.182", "type": "public"},
                    ]
                },
            }
        return droplet

    def list_droplets(self):
        return list(self.droplets.values())

    def delete_droplet(self, droplet_id):
        self.droplets_deleted.append(str(droplet_id))
        self.droplets.pop(str(droplet_id), None)


def _backend(client: FakeDigitalOceanClient) -> DigitalOceanSandboxBackend:
    config = DigitalOceanSandboxConfig(cloud=DigitalOceanCloudConfig(token="t"))
    return DigitalOceanSandboxBackend(config=config, client=client)


def _request(**overrides) -> SandboxRequest:
    fields = {
        "experiment_id": "exp_1",
        "project_id": "proj_1",
        "public_key": "ssh-ed25519 AAAA old",
        "sandbox_uid": "uid123",
        "instance_type": "gpu-h100x1-80gb",
    }
    fields.update(overrides)
    return SandboxRequest(**fields)


class DigitalOceanAcquireTest(unittest.TestCase):
    def test_acquire_registers_key_and_returns_public_ipv4(self) -> None:
        client = FakeDigitalOceanClient()
        backend = _backend(client)
        with patch.object(DigitalOceanSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        self.assertEqual(provisioned.sandbox_id, "3164444")
        self.assertEqual(provisioned.ssh_host, "104.236.32.182")  # public, not private
        self.assertEqual(provisioned.ssh_user, "root")
        self.assertEqual(provisioned.region, "nyc2")  # deterministic first region
        self.assertEqual(provisioned.price_usd_per_hour, 3.39)
        self.assertEqual(provisioned.gpu, "H100")
        created = client.droplets_created[0]
        self.assertEqual(created["image"], "gpu-h100x1-base")
        self.assertEqual(created["ssh_key_ids"], [512190])
        self.assertIn("#!/usr/bin/env bash", created["user_data"])

    def test_acquire_reuses_deduped_fingerprint_key(self) -> None:
        client = FakeDigitalOceanClient()
        client.duplicate_key = True
        backend = _backend(client)
        with patch.object(DigitalOceanSandboxBackend, "_wait_for_ssh"):
            provisioned = backend.acquire(request=_request())

        # 422 duplicate resolves to the stored key's id instead of failing.
        self.assertEqual(client.droplets_created[0]["ssh_key_ids"], [999])
        self.assertTrue(provisioned.sandbox_id)

    def test_acquire_unknown_size_lists_visible_gpu_sizes(self) -> None:
        backend = _backend(FakeDigitalOceanClient())
        with self.assertRaisesRegex(BackendValidationError, "gpu-h100x1-80gb"):
            backend.acquire(request=_request(instance_type="gpu-mi300x1-192gb"))

    def test_acquire_unavailable_size_raises_capacity_error(self) -> None:
        backend = _backend(FakeDigitalOceanClient())
        with self.assertRaises(CapacityUnavailableError):
            backend.acquire(request=_request(instance_type="gpu-h100x8-640gb"))

    def test_acquire_failure_destroys_droplet_and_key(self) -> None:
        client = FakeDigitalOceanClient()
        backend = _backend(client)
        with patch.object(
            DigitalOceanSandboxBackend,
            "_wait_for_active_droplet",
            side_effect=BackendUnavailableError("boom"),
        ):
            with self.assertRaises(BackendUnavailableError):
                backend.acquire(request=_request())

        self.assertEqual(client.droplets_deleted, ["3164444"])
        self.assertEqual(client.keys_deleted, [512190])


class DigitalOceanLivenessTest(unittest.TestCase):
    def test_404_is_authoritatively_dead(self) -> None:
        backend = _backend(FakeDigitalOceanClient())
        self.assertFalse(backend.is_alive(sandbox_id="999"))

    def test_off_droplet_still_counts_as_alive(self) -> None:
        # Powered-off droplets still bill; only destroy stops charges.
        client = FakeDigitalOceanClient()
        client.droplets["7"] = {"id": 7, "status": "off", "networks": {"v4": []}}
        client.get_calls = -100  # keep the stored status
        backend = _backend(client)
        self.assertTrue(backend.is_alive(sandbox_id="7"))

    def test_outage_raises_rather_than_reporting_dead(self) -> None:
        class OutageClient(FakeDigitalOceanClient):
            def get_droplet(self, droplet_id):
                raise BackendUnavailableError("gateway timeout", status=502)

        backend = _backend(OutageClient())
        with self.assertRaises(BackendUnavailableError):
            backend.is_alive(sandbox_id="7")

    def test_terminate_treats_404_as_already_gone(self) -> None:
        backend = _backend(FakeDigitalOceanClient())
        self.assertTrue(backend.terminate(sandbox_id="404404"))


class DigitalOceanCatalogTest(unittest.TestCase):
    def test_options_offer_only_available_gpu_sizes(self) -> None:
        options = to_agent_options(SIZES)

        self.assertEqual([o["instance_type"] for o in options], ["gpu-h100x1-80gb"])
        option = options[0]
        self.assertEqual(option["gpu"], "H100")
        self.assertEqual(option["gpu_count"], 1)
        self.assertEqual(option["memory_gib"], 240)
        self.assertEqual(option["regions"], ["nyc2", "tor1"])

    def test_empty_menu_surfaces_the_account_unlock_gotcha(self) -> None:
        class NoGpuClient(FakeDigitalOceanClient):
            def list_sizes(self):
                return [s for s in SIZES if "gpu_info" not in s]

        backend = _backend(NoGpuClient())
        catalog = backend.hardware_catalog()

        self.assertEqual(catalog["options"], [])
        self.assertIn("unlock", catalog["reason"])


if __name__ == "__main__":
    unittest.main()
