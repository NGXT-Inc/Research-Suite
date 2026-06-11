from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.execution.errors import BackendUnavailableError, BackendValidationError
from backend.execution.types import SandboxRequest
from backend.execution.backends.modal.config import ModalConfig
from backend.execution.backends.modal.sandbox_backend import ModalSandboxBackend
from tests.fakes import FakeProcess


# --- fake modal SDK ---------------------------------------------------------


class FakeImage:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.apt_packages: list[str] = []
        self.pip_packages: list[str] = []

    @classmethod
    def debian_slim(cls, **_kw) -> "FakeImage":
        return cls()

    @classmethod
    def from_registry(cls, *_a, **_kw) -> "FakeImage":
        return cls()

    def apt_install(self, *_a) -> "FakeImage":
        self.apt_packages.extend(str(pkg) for pkg in _a)
        return self

    def pip_install(self, *_a) -> "FakeImage":
        self.pip_packages.extend(str(pkg) for pkg in _a)
        return self

    def run_commands(self, *cmds) -> "FakeImage":
        self.commands.extend(cmds)
        return self


class FakeTunnel:
    """Mimics the SSH (TCP) tunnel shape — exposes .tcp_socket."""

    def __init__(self, host: str, port: int) -> None:
        self.tcp_socket = (host, port)


class FakeEncryptedTunnel:
    """Mimics the dashboard (HTTPS) tunnel shape — exposes .url."""

    def __init__(self, url: str) -> None:
        self.url = url


class FakeSandbox:
    registry: dict[str, "FakeSandbox"] = {}
    tunnels_fail = False

    def __init__(self, object_id: str) -> None:
        self.object_id = object_id
        self.tags: dict[str, str] = {}
        self._poll = None
        self.terminated = False
        self.exec_calls: list[str] = []
        self.transcript = ""
        self.metrics_output = ""

    def tunnels(self):
        if FakeSandbox.tunnels_fail:
            raise RuntimeError("tunnel not ready")
        return {
            22: FakeTunnel("sandbox.modal.test", 50022),
            5000: FakeEncryptedTunnel(
                f"https://mlflow-{self.object_id}.modal.test"
            ),
            6006: FakeEncryptedTunnel(
                f"https://tensorboard-{self.object_id}.modal.test"
            ),
        }

    def set_tags(self, tags) -> None:
        self.tags = dict(tags)

    def poll(self):
        return self._poll

    def terminate(self) -> None:
        self.terminated = True
        self._poll = 0

    def detach(self) -> None:
        pass

    def exec(self, *args, timeout=None):
        command = " ".join(str(a) for a in args)
        self.exec_calls.append(command)
        # The usage sampler is the only exec that shells out to nvidia-smi.
        if "nvidia-smi" in command:
            return FakeProcess(stdout=self.metrics_output, code=0)
        return FakeProcess(stdout=self.transcript, code=0)


class FakeSandboxClass:
    created: list[dict] = []
    by_name: dict[str, "FakeSandbox"] = {}

    @classmethod
    def create(cls, *args, **kwargs):
        sandbox = FakeSandbox(object_id=f"sb_{len(FakeSandbox.registry) + 1}")
        cls.created.append({"args": args, "kwargs": kwargs})
        FakeSandbox.registry[sandbox.object_id] = sandbox
        name = kwargs.get("name")
        if name:
            cls.by_name[name] = sandbox
        return sandbox

    @classmethod
    def from_id(cls, sandbox_id):
        return FakeSandbox.registry[sandbox_id]

    @classmethod
    def from_name(cls, app_name, name, **_kw):
        return cls.by_name[name]  # raises KeyError when absent


class FakeSecret:
    @staticmethod
    def from_dict(d):
        return {"secret": dict(d)}

    @staticmethod
    def from_local_environ(keys):
        return {"local_environ": list(keys), "secret": {key: os.environ[key] for key in keys}}


class FakeApp:
    @staticmethod
    def lookup(name, create_if_missing=False):
        return type("App", (), {"app_id": "app_1", "name": name})()


class FakeModal:
    Image = FakeImage
    Sandbox = FakeSandboxClass
    Secret = FakeSecret
    App = FakeApp


class ModalSandboxBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeSandbox.registry = {}
        FakeSandbox.tunnels_fail = False
        FakeSandboxClass.created = []
        FakeSandboxClass.by_name = {}
        self._old_hf_env = {
            "HF_TOKEN": os.environ.get("HF_TOKEN"),
            "HUGGING_FACE_HUB_TOKEN": os.environ.get("HUGGING_FACE_HUB_TOKEN"),
        }
        os.environ["MODAL_TOKEN_ID"] = os.environ.get("MODAL_TOKEN_ID", "tok")
        os.environ["MODAL_TOKEN_SECRET"] = os.environ.get("MODAL_TOKEN_SECRET", "sec")
        os.environ.pop("HF_TOKEN", None)
        os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
        self.tmp = tempfile.TemporaryDirectory()
        config = ModalConfig.from_env()
        # ModalConfig.from_env() intentionally loads the plugin .env. Most tests
        # exercise sandbox creation without optional Hugging Face credentials, so
        # clear them after config loading and restore the developer environment in
        # tearDown.
        os.environ.pop("HF_TOKEN", None)
        os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
        self.backend = ModalSandboxBackend(
            repo_root=Path(self.tmp.name),
            config=config,
            modal_module=FakeModal,
        )

    def tearDown(self) -> None:
        for key, value in self._old_hf_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _request(self) -> SandboxRequest:
        return SandboxRequest(
            experiment_id="exp1",
            project_id="proj1",
            public_key="ssh-ed25519 AAAA",
            gpu="A100",
            time_limit=1234,
        )

    def test_acquire_wires_ssh_tunnel(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        self.assertEqual(provisioned.ssh_host, "sandbox.modal.test")
        self.assertEqual(provisioned.ssh_port, 50022)
        self.assertEqual(provisioned.ssh_user, "root")
        self.assertFalse(provisioned.reused)

        create = FakeSandboxClass.created[-1]
        # boot entrypoint
        self.assertEqual(create["args"], ("bash", "/opt/rp/boot.sh"))
        kwargs = create["kwargs"]
        self.assertEqual(kwargs["unencrypted_ports"], [22])
        # Dashboards: MLflow on 5000 and TensorBoard on 6006 are requested as
        # encrypted_ports so Modal exposes them as HTTPS tunnels.
        self.assertEqual(kwargs["encrypted_ports"], [5000, 6006])
        self.assertEqual(kwargs["timeout"], 1234)
        self.assertEqual(kwargs["gpu"], "A100")
        self.assertNotIn("volumes", kwargs)
        self.assertEqual(provisioned.sync_dir, kwargs["workdir"])
        self.assertEqual(provisioned.unsynced_dir, self.backend.config.sandbox_data_dir)
        self.assertEqual(provisioned.sandbox_data_dir, self.backend.config.sandbox_data_dir)
        self.assertEqual(provisioned.volume_name, "")
        self.assertNotIn("secrets", kwargs)
        self.assertEqual(kwargs["env"]["RP_SYNC_DIR"], kwargs["workdir"])
        self.assertEqual(kwargs["env"]["RP_UNSYNCED_DIR"], self.backend.config.sandbox_data_dir)
        self.assertEqual(kwargs["env"]["RP_SANDBOX_DATA_DIR"], self.backend.config.sandbox_data_dir)
        self.assertEqual(kwargs["env"]["RP_EXPERIMENT_ID"], "exp1")
        self.assertNotIn("HF_TOKEN", kwargs["env"])
        # tags applied
        sandbox = FakeSandbox.registry[provisioned.sandbox_id]
        self.assertEqual(sandbox.tags["experiment_id"], "exp1")
        self.assertEqual(sandbox.tags["research_plugin_role"], "sandbox")

    def test_acquire_captures_dashboard_urls(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        # ProvisionedSandbox.dashboards mirrors the encrypted-tunnel .url fields
        # for MLflow (5000) and TensorBoard (6006). Names — not ports — are the
        # contract surface so future dashboards key cleanly.
        self.assertEqual(set(provisioned.dashboards.keys()), {"mlflow", "tensorboard"})
        self.assertTrue(provisioned.dashboards["mlflow"].startswith("https://mlflow-"))
        self.assertTrue(
            provisioned.dashboards["tensorboard"].startswith("https://tensorboard-")
        )

    def test_dashboard_urls_refresh_returns_fresh_map(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        # The registry calls this after detecting tunnel relocation; mirrors
        # refresh_ssh_endpoint's shape but for HTTPS dashboard tunnels.
        urls = self.backend.dashboard_urls(sandbox_id=provisioned.sandbox_id)
        self.assertEqual(urls, dict(provisioned.dashboards))

    def test_dashboard_urls_empty_when_sandbox_id_missing(self) -> None:
        # Defensive: empty dict, never raises — same contract as
        # refresh_ssh_endpoint returning None when there's nothing to refresh.
        self.assertEqual(self.backend.dashboard_urls(sandbox_id=""), {})

    def test_boot_script_starts_mlflow_and_tensorboard(self) -> None:
        # The image layering writes the boot script as a heredoc into the
        # image; the embedded module-level BOOT_SCRIPT is the source of truth.
        from backend.execution.backends.modal.sandbox_backend import BOOT_SCRIPT, REC_SCRIPT

        # MLflow tracking server bound to 0.0.0.0:5000, SQLite backend store,
        # artifacts under the per-experiment sessions dir on the Volume.
        self.assertIn("mlflow server", BOOT_SCRIPT)
        self.assertIn("--port 5000", BOOT_SCRIPT)
        self.assertIn("backend-store-uri", BOOT_SCRIPT)
        # TensorBoard, same Volume-backed logdir pattern.
        self.assertIn("tensorboard.main", BOOT_SCRIPT)
        self.assertIn("--port 6006", BOOT_SCRIPT)
        # Both must be backgrounded so sshd still becomes the foreground PID 1.
        self.assertIn("&\n", BOOT_SCRIPT)
        # MLFLOW_TRACKING_URI must reach every SSH command so framework
        # auto-detection (HF Trainer's report_to="all") just works.
        self.assertIn("MLFLOW_TRACKING_URI", BOOT_SCRIPT)
        self.assertIn("MLFLOW_TRACKING_URI", REC_SCRIPT)

    def test_modal_images_install_agent_shell_baseline(self) -> None:
        base = self.backend._base_image_default()
        cuda = self.backend._cuda_image_default()

        expected = {
            "ripgrep",
            "fd-find",
            "jq",
            "rsync",
            "tree",
            "git-lfs",
            "build-essential",
            "ninja-build",
            "lsof",
        }
        self.assertTrue(expected.issubset(set(base.apt_packages)))
        self.assertTrue(expected.issubset(set(cuda.apt_packages)))
        self.assertIn("ln -sf /usr/bin/fdfind /usr/local/bin/fd || true", base.commands)
        self.assertIn("ln -sf /usr/bin/fdfind /usr/local/bin/fd || true", cuda.commands)

    def test_huggingface_token_is_passed_as_secret_env(self) -> None:
        with mock.patch.dict(os.environ, {"HF_TOKEN": "hf_test_secret"}, clear=False):
            self.backend.acquire(request=self._request())
            env_info = self.backend.sandbox_environment()

        secrets = FakeSandboxClass.created[-1]["kwargs"]["secrets"]
        self.assertEqual(secrets[0]["local_environ"], ["HF_TOKEN"])
        secret = secrets[0]["secret"]
        self.assertEqual(secret["HF_TOKEN"], "hf_test_secret")
        self.assertNotIn("HUGGING_FACE_HUB_TOKEN", secret)
        self.assertNotIn("HF_TOKEN", FakeSandboxClass.created[-1]["kwargs"]["env"])
        self.assertIn("HF_TOKEN", env_info["available_tokens"])
        self.assertNotIn("hf_test_secret", str(env_info))

    def test_acquire_invokes_phase_and_created_callbacks(self) -> None:
        phases: list[str] = []
        created: list[tuple[str, str]] = []
        provisioned = self.backend.acquire(
            request=self._request(),
            on_phase=lambda p, _d: phases.append(p),
            on_created=lambda sid, name: created.append((sid, name)),
        )
        # the registry relies on these phases for visibility
        self.assertEqual(phases[0], "creating")
        self.assertIn("creating", phases)
        self.assertEqual(phases[-1], "connecting")
        # on_created fires once the sandbox exists, before the tunnel wait
        self.assertEqual(len(created), 1)
        sid, name = created[0]
        self.assertEqual(sid, provisioned.sandbox_id)
        self.assertEqual(name, "rp-exp1")

    def test_acquire_terminates_sandbox_on_tunnel_failure(self) -> None:
        FakeSandbox.tunnels_fail = True
        with self.assertRaises(BackendUnavailableError):
            self.backend.acquire(request=self._request())
        # the created sandbox must be terminated — no orphan holds the name
        sandboxes = list(FakeSandbox.registry.values())
        self.assertTrue(sandboxes)
        self.assertTrue(sandboxes[-1].terminated)

    def test_acquire_cancel_via_on_created_terminates(self) -> None:
        def cancel(_sid, _name):
            raise RuntimeError("canceled")

        with self.assertRaises(RuntimeError):
            self.backend.acquire(request=self._request(), on_created=cancel)
        sandboxes = list(FakeSandbox.registry.values())
        self.assertTrue(sandboxes[-1].terminated)

    def test_read_transcript_live(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        FakeSandbox.registry[provisioned.sandbox_id].transcript = "epoch 1 loss 0.5\n"
        text = self.backend.read_transcript(
            sandbox_id=provisioned.sandbox_id,
            experiment_id="exp1",
            volume_name=provisioned.volume_name,
            workdir=provisioned.workdir,
        )
        self.assertIn("epoch 1 loss 0.5", text)

    def test_read_transcript_without_volume_fallback_returns_empty(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        FakeSandbox.registry[provisioned.sandbox_id].transcript = ""
        text = self.backend.read_transcript(
            sandbox_id=provisioned.sandbox_id,
            experiment_id="exp1",
            volume_name=provisioned.volume_name,
            workdir=provisioned.workdir,
        )
        self.assertEqual(text, "")

    def test_config_rejects_unsynced_dir_inside_sync_dir(self) -> None:
        with self.assertRaises(BackendValidationError):
            ModalConfig(
                app_name="research-plugin-jobs",
                retention_seconds=600,
                sandbox_timeout=4200,
                job_timeout=3000,
                idle_timeout=0,
                remote_workdir="/workspace/synced",
                sandbox_data_dir="/workspace/synced/cache",
                runner_dir="/workspace/synced/.research_plugin_job",
            ).validated()

    def test_sample_metrics_parses_gauges(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        FakeSandbox.registry[provisioned.sandbox_id].metrics_output = (
            "RPM cpu_cores_used=1.5000\n"
            "RPM cpu_cores_limit=2.0000\n"
            "RPM mem_used_bytes=2147483648\n"
            "RPM mem_limit_bytes=8589934592\n"
            "RPM gpu idx=0 util=42 used=1024 total=40960 name=NVIDIA A100-SXM4-40GB\n"
            "RPM ok=1\n"
        )
        metrics = self.backend.sample_metrics(sandbox_id=provisioned.sandbox_id)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["cpu"], {"used_cores": 1.5, "limit_cores": 2.0})
        self.assertEqual(metrics["memory"], {"used_bytes": 2147483648, "limit_bytes": 8589934592})
        self.assertEqual(len(metrics["gpus"]), 1)
        gpu = metrics["gpus"][0]
        self.assertEqual(gpu["util_pct"], 42)
        self.assertEqual(gpu["mem_used_mib"], 1024)
        self.assertEqual(gpu["mem_total_mib"], 40960)
        self.assertIn("A100", gpu["name"])

if __name__ == "__main__":
    unittest.main()
