from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from merv.brain.sandbox.execution import build_sandbox_backend
from merv.brain.sandbox.execution.driver_registry import sandbox_driver_descriptor
from merv.brain.sandbox.execution.backends.thunder_compute.catalog import summarize_specs
from merv.brain.sandbox.execution.backends.thunder_compute.config import (
    ThunderCloudConfig,
    ThunderSandboxConfig,
)
from merv.brain.sandbox.execution.backends.thunder_compute.sandbox_backend import (
    ThunderComputeSandboxBackend,
    build_thunder_bootstrap_script,
)
from merv.brain.sandbox.execution.vm_bootstrap import MGMT_SSH_USER, REC_SCRIPT
from merv.brain.sandbox.execution.vm_ssh import TRANSCRIPT_TAIL_DEFAULT
from merv.brain.sandbox.sandbox_backend import (
    BackendUnavailableError,
    BackendValidationError,
    SandboxRequest,
)
from tests.sandbox.driver_conformance import (
    assert_catalog_envelope,
    assert_driver_surface,
)


SPECS = {
    "a100xl_x1_prototyping": {
        "displayName": "NVIDIA A100 (80GB)",
        "vramGB": 80,
        "gpuCount": 1,
        "mode": "prototyping",
        "vcpuOptions": [4, 8, 12],
        "ramPerVCPUGiB": 8,
        "storageGB": {"min": 100, "max": 500},
    },
    "h100_x1_prototyping": {
        "displayName": "NVIDIA H100",
        "vramGB": 80,
        "gpuCount": 1,
        "mode": "prototyping",
        "vcpuOptions": [4, 8],
        "ramPerVCPUGiB": 8,
        "storageGB": {"min": 100, "max": 500},
    },
}
PRICING = {
    "a100xl_x1_prototyping": 0.78,
    "h100_x1_prototyping": 1.38,
}


class FakeThunderClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.list_calls = 0

    def list_specs(self):
        return SPECS

    def pricing(self):
        return PRICING

    def create_instance(self, **kwargs):
        self.created.append(kwargs)
        return {"identifier": 7, "uuid": "uuid-7", "key": "unused-generated-key"}

    def list_instances(self):
        self.list_calls += 1
        if self.list_calls == 1:
            return {
                "7": {
                    "uuid": "uuid-7",
                    "status": "STARTING",
                    "ip": "",
                    "port": 31995,
                }
            }
        return {
            "7": {
                "uuid": "uuid-7",
                "status": "RUNNING",
                "ip": "198.51.100.7",
                "port": 31995,
                "gpuType": "A100XL",
                "numGpus": "1",
            }
        }

    def delete_instance(self, instance_id: str):
        self.deleted.append(instance_id)


class FakeBootstrapRunner:
    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[dict] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command: list[str], script: str, timeout: int):
        self.calls.append({"command": list(command), "script": script, "timeout": timeout})
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


class FakeSshRunner:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.commands: list[list[str]] = []
        self.inputs: list[str] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, command: list[str], stdin: str | None = None):
        self.commands.append(list(command))
        if stdin is not None:
            self.inputs.append(stdin)
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


class ThunderCatalogTest(unittest.TestCase):
    def test_catalog_normalizes_specs_cheapest_first(self) -> None:
        result = summarize_specs(SPECS, pricing=PRICING, template="base")

        self.assertEqual(result["provider"], "thunder_compute")
        self.assertEqual(result["count"], 2)
        first = result["instance_types"][0]
        self.assertEqual(first["instance_type"], "a100xl_x1_prototyping")
        self.assertEqual(first["gpu_type"], "a100xl")
        self.assertEqual(first["gpu_count"], 1)
        self.assertEqual(first["vcpus"], 4)
        self.assertEqual(first["vcpu_options"], [4, 8, 12])
        self.assertEqual(first["memory_gib"], 32)
        self.assertEqual(first["storage_gib"], 100)
        self.assertEqual(first["price_usd_per_hour"], 0.78)

    def test_catalog_filters_by_gpu(self) -> None:
        result = summarize_specs(SPECS, pricing=PRICING, template="base", gpu="h100")
        names = [row["instance_type"] for row in result["instance_types"]]
        self.assertEqual(names, ["h100_x1_prototyping"])


class ThunderConfigTest(unittest.TestCase):
    def test_env_config_accepts_thunder_compute_api_key(self) -> None:
        with patch.dict(os.environ, {"THUNDER_COMPUTE_API_KEY": "test-key"}, clear=True):
            config = ThunderCloudConfig.from_env()
        self.assertEqual(config.api_key, "test-key")
        self.assertEqual(config.base_url, "https://api.thundercompute.com:8443/v1")

    def test_env_config_loads_plugin_env_file_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("THUNDER_COMPUTE_API_KEY=file-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"RESEARCH_PLUGIN_THUNDER_ENV_FILE": str(env_file)}, clear=True):
                config = ThunderCloudConfig.from_env()
        self.assertEqual(config.api_key, "file-key")

    def test_env_config_requires_api_key(self) -> None:
        with patch.dict(os.environ, {"RESEARCH_PLUGIN_MODE": "control"}, clear=True):
            with self.assertRaises(BackendValidationError):
                ThunderCloudConfig.from_env()

    def test_env_config_rejects_plain_http_remote_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "THUNDER_COMPUTE_API_KEY": "test-key",
                "RESEARCH_PLUGIN_THUNDER_API_BASE": "http://api.thundercompute.com:8443/v1",
            },
            clear=True,
        ):
            with self.assertRaises(BackendValidationError):
                ThunderCloudConfig.from_env()

    def test_env_config_allows_plain_http_localhost_base_url_for_tests(self) -> None:
        with patch.dict(
            os.environ,
            {
                "THUNDER_COMPUTE_API_KEY": "test-key",
                "RESEARCH_PLUGIN_THUNDER_API_BASE": "http://localhost:8443/v1",
            },
            clear=True,
        ):
            config = ThunderCloudConfig.from_env()
        self.assertEqual(config.base_url, "http://localhost:8443/v1")


class ThunderBackendTest(unittest.TestCase):
    def _backend(
        self,
        *,
        client: FakeThunderClient | None = None,
        bootstrap_runner: FakeBootstrapRunner | None = None,
        ssh_runner: FakeSshRunner | None = None,
    ) -> tuple[ThunderComputeSandboxBackend, FakeThunderClient, FakeBootstrapRunner, FakeSshRunner]:
        fake_client = client or FakeThunderClient()
        fake_bootstrap = bootstrap_runner or FakeBootstrapRunner()
        fake_ssh = ssh_runner or FakeSshRunner()
        config = ThunderSandboxConfig(
            cloud=ThunderCloudConfig(api_key="test-key"),
            poll_interval_seconds=0.001,
            poll_timeout_seconds=1,
        )
        backend = ThunderComputeSandboxBackend(
            config=config,
            client=fake_client,  # type: ignore[arg-type]
            ssh_runner=fake_ssh,
            ssh_input_runner=fake_ssh,
            bootstrap_runner=fake_bootstrap,
        )
        return backend, fake_client, fake_bootstrap, fake_ssh

    def _request(self, **overrides) -> SandboxRequest:
        kwargs = {
            "experiment_id": "exp1",
            "project_id": "proj1",
            "public_key": "ssh-ed25519 AAAAuser test",
            "management_public_key": "ssh-ed25519 AAAAmgmt test",
            "management_key_path": "/keys/mgmt",
            "instance_type": "a100xl_x1_prototyping",
        }
        kwargs.update(overrides)
        return SandboxRequest(**kwargs)

    def test_shared_driver_contract_with_injected_client(self) -> None:
        backend, _, _, _ = self._backend()
        descriptor = sandbox_driver_descriptor("thunder_compute")

        assert_driver_surface(self, descriptor=descriptor, backend=backend)
        assert_catalog_envelope(self, descriptor=descriptor, backend=backend)

    def test_build_sandbox_backend_accepts_thunder_aliases(self) -> None:
        with patch.dict(os.environ, {"RESEARCH_PLUGIN_EXECUTION_BACKEND": "thunder"}, clear=True):
            aliased = build_sandbox_backend(repo_root=Path("/tmp/merv-test"))
        self.assertEqual(aliased.capabilities.name, "thunder_compute")

        with patch.dict(
            os.environ,
            {"RESEARCH_PLUGIN_EXECUTION_BACKEND": "thunder_compute"},
            clear=True,
        ):
            canonical = build_sandbox_backend(repo_root=Path("/tmp/merv-test"))
        self.assertEqual(canonical.capabilities.name, "thunder_compute")

    def test_hardware_catalog_lists_thunder_options(self) -> None:
        backend, _, _, _ = self._backend()
        catalog = backend.hardware_catalog()

        self.assertEqual(catalog["provider"], "thunder_compute")
        self.assertTrue(catalog["selection_required"])
        self.assertEqual(catalog["select_with"], "instance_type")
        self.assertEqual(catalog["regions"], [])
        self.assertEqual(catalog["options"][0]["instance_type"], "a100xl_x1_prototyping")

    def test_find_sandbox_id_matches_live_instance_by_management_key_comment(self) -> None:
        client = FakeThunderClient()

        def list_instances():
            return {
                "7": {
                    "status": "terminated",
                    "sshPublicKeys": ["ssh-ed25519 AAAAmgmt merv-mgmt-exp1"],
                },
                "8": {
                    "status": "running",
                    "sshPublicKeys": [
                        {"publicKey": "ssh-ed25519 AAAAmgmt merv-mgmt-exp1"}
                    ],
                },
            }

        client.list_instances = list_instances
        backend, _, _, _ = self._backend(client=client)

        self.assertEqual(backend.find_sandbox_id(experiment_id="exp1"), "8")
        self.assertIsNone(backend.find_sandbox_id(experiment_id="other"))

    def test_acquire_creates_vm_without_user_data_then_bootstraps_over_ssh(self) -> None:
        backend, client, bootstrap, ssh = self._backend()

        provisioned = backend.acquire(request=self._request())

        self.assertEqual(provisioned.sandbox_id, "7")
        self.assertEqual(provisioned.ssh_host, "198.51.100.7")
        self.assertEqual(provisioned.ssh_port, 31995)
        self.assertEqual(provisioned.ssh_user, "ubuntu")
        self.assertEqual(provisioned.workdir, "/workspace/exp1")
        self.assertEqual(provisioned.sync_dir, "/workspace/exp1")
        self.assertEqual(provisioned.gpu, "NVIDIA A100 (80GB)")
        self.assertEqual(provisioned.cpu, 4.0)
        self.assertEqual(provisioned.memory, 32 * 1024)
        create = client.created[0]
        self.assertEqual(create["cpu_cores"], 4)
        self.assertEqual(create["disk_size_gb"], 100)
        self.assertEqual(create["gpu_type"], "a100xl")
        self.assertEqual(create["mode"], "prototyping")
        self.assertEqual(create["num_gpus"], 1)
        self.assertEqual(create["template"], "base")
        self.assertEqual(create["public_key"], "ssh-ed25519 AAAAmgmt test")
        self.assertNotIn("user_data", create)
        self.assertEqual(bootstrap.calls[0]["command"][-2], "ubuntu@198.51.100.7")
        self.assertEqual(bootstrap.calls[0]["command"][-1], "sudo -n bash -s")
        self.assertIn("/keys/mgmt", bootstrap.calls[0]["command"])
        script = bootstrap.calls[0]["script"]
        self.assertIn("MERV_EXPERIMENT_DIR=/workspace/exp1", script)
        self.assertNotIn("MLFLOW_TRACKING_URI", script)
        self.assertNotIn("RP_EXECUTION_BACKEND", script)
        self.assertIn("ForceCommand /opt/merv/rec.sh", script)
        self.assertNotIn("/opt/merv/parachute.sh", script)
        # Bootstrap readiness is checked through the management principal.
        self.assertEqual(ssh.commands[-1][-2], f"{MGMT_SSH_USER}@198.51.100.7")

    def test_acquire_calls_created_before_bootstrap_and_cleans_up_on_bootstrap_failure(self) -> None:
        events: list[tuple] = []
        backend, client, _, _ = self._backend(
            bootstrap_runner=FakeBootstrapRunner(returncode=1, stderr="boom")
        )

        with self.assertRaises(BackendUnavailableError):
            backend.acquire(
                request=self._request(),
                on_created=lambda sandbox_id, name: events.append((sandbox_id, name)),
            )

        self.assertEqual(events, [("7", "uuid-7")])
        self.assertEqual(client.deleted, ["7"])

    def test_acquire_cancellation_after_create_terminates_vm(self) -> None:
        backend, client, bootstrap, _ = self._backend()

        def cancel(_sandbox_id, _name):
            raise RuntimeError("cancel")

        with self.assertRaises(RuntimeError):
            backend.acquire(request=self._request(), on_created=cancel)

        self.assertEqual(client.deleted, ["7"])
        self.assertEqual(bootstrap.calls, [])

    def test_acquire_cancellation_during_poll_terminates_vm(self) -> None:
        backend, client, bootstrap, _ = self._backend()

        def cancel_during_poll(_phase: str, detail: str) -> None:
            if detail.startswith("Thunder instance status:"):
                raise RuntimeError("cancel")

        with self.assertRaises(RuntimeError):
            backend.acquire(request=self._request(), on_phase=cancel_during_poll)

        self.assertEqual(client.deleted, ["7"])
        self.assertEqual(bootstrap.calls, [])

    def test_acquire_rejects_missing_management_key(self) -> None:
        backend, client, _, _ = self._backend()
        with self.assertRaises(BackendValidationError):
            backend.acquire(request=self._request(management_key_path=""))
        self.assertEqual(client.created, [])

    def test_read_transcript_uses_management_channel(self) -> None:
        transcript = "[t] $ echo hi\nhi\n[t] (exit 0)\n"
        data = transcript.encode("utf-8")
        wire = f"{len(data)}\n" + base64.encodebytes(data).decode("ascii")
        backend, _, _, ssh = self._backend(ssh_runner=FakeSshRunner(stdout=wire))

        tail = backend.read_transcript(
            sandbox_id="7",
            experiment_id="exp1",
            volume_name="",
            workdir="/workspace/exp1",
            ssh_host="198.51.100.7",
            ssh_port=31995,
            ssh_user="ubuntu",
            key_path="/keys/mgmt",
        )

        self.assertEqual(tail.data, data)
        self.assertEqual(tail.total_bytes, len(data))
        command = ssh.commands[0]
        self.assertEqual(command[-2], f"{MGMT_SSH_USER}@198.51.100.7")
        self.assertIn("/keys/mgmt", command)
        self.assertIn(f"tail -c {TRANSCRIPT_TAIL_DEFAULT}", command[-1])

    def test_sample_metrics_returns_none_on_ssh_failure(self) -> None:
        backend, _, _, _ = self._backend(ssh_runner=FakeSshRunner(returncode=255))

        self.assertIsNone(
            backend.sample_metrics(
                sandbox_id="7",
                ssh_host="198.51.100.7",
                ssh_port=31995,
                key_path="/keys/mgmt",
            )
        )

    def test_write_secrets_keeps_token_out_of_argv(self) -> None:
        backend, _, _, ssh = self._backend()

        self.assertTrue(
            backend.write_secrets(
                sandbox_id="7",
                secrets={"HF_TOKEN": "hf_secret_value"},
                ssh_host="198.51.100.7",
                ssh_port=31995,
                key_path="/keys/mgmt",
            )
        )
        command = ssh.commands[0]
        self.assertEqual(command[-2], f"{MGMT_SSH_USER}@198.51.100.7")
        argv = " ".join(command)
        self.assertNotIn("hf_secret_value", argv)
        self.assertNotIn(
            base64.b64encode(b"export HF_TOKEN=hf_secret_value\n").decode("ascii"),
            argv,
        )
        self.assertEqual(ssh.inputs, ["export HF_TOKEN=hf_secret_value\n"])
        self.assertIn("/opt/merv/secrets.env", command[-1])

    def test_bootstrap_script_contains_rec_bypass_and_no_plain_tokens(self) -> None:
        script = build_thunder_bootstrap_script(
            public_key="ssh-ed25519 AAAAuser test",
            management_public_key="ssh-ed25519 AAAAmgmt test",
            experiment_id="exp1",
            workdir="/workspace/exp1",
            sessions_dir="/workspace/.merv_sessions/exp1",
            sandbox_data_dir="/workspace/data",
        )
        self.assertIn("rsync\\ --server*", REC_SCRIPT)
        self.assertLess(REC_SCRIPT.index("rsync"), REC_SCRIPT.index("tmux new-session"))
        self.assertIn("systemctl reload ssh", script)
        self.assertIn("tmux", script)
        self.assertNotIn("HF_TOKEN=", script)


if __name__ == "__main__":
    unittest.main()
