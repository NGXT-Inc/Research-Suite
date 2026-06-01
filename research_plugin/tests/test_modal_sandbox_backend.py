from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.execution.errors import BackendUnavailableError
from backend.execution.types import SandboxRequest
from backend.execution.backends.modal.config import ModalConfig
from backend.execution.backends.modal.sandbox_backend import ModalSandboxBackend


# --- fake modal SDK ---------------------------------------------------------


class FakeImage:
    def __init__(self) -> None:
        self.commands: list[str] = []

    @classmethod
    def debian_slim(cls, **_kw) -> "FakeImage":
        return cls()

    @classmethod
    def from_registry(cls, *_a, **_kw) -> "FakeImage":
        return cls()

    def apt_install(self, *_a) -> "FakeImage":
        return self

    def pip_install(self, *_a) -> "FakeImage":
        return self

    def run_commands(self, *cmds) -> "FakeImage":
        self.commands.extend(cmds)
        return self


class FakeProcess:
    def __init__(self, stdout: str = "", code: int = 0) -> None:
        self._stdout = stdout
        self._code = code

    @property
    def stdout(self):
        text = self._stdout

        class _S:
            def read(self_inner):
                return text

        return _S()

    @property
    def stderr(self):
        return None

    def wait(self) -> int:
        return self._code


class FakeTunnel:
    def __init__(self, host: str, port: int) -> None:
        self.tcp_socket = (host, port)


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

    def tunnels(self):
        if FakeSandbox.tunnels_fail:
            raise RuntimeError("tunnel not ready")
        return {22: FakeTunnel("sandbox.modal.test", 50022)}

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
        self.exec_calls.append(" ".join(str(a) for a in args))
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


class FakeVolume:
    def __init__(self, name: str) -> None:
        self.name = name
        self.files: dict[str, bytes] = {}

    def reload(self) -> None:
        pass

    def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        yield self.files[path]


class FakeVolumeClass:
    instances: dict[str, FakeVolume] = {}

    @classmethod
    def from_name(cls, name, create_if_missing=False):
        return cls.instances.setdefault(name, FakeVolume(name))


class FakeSecret:
    @staticmethod
    def from_dict(d):
        return {"secret": dict(d)}


class FakeApp:
    @staticmethod
    def lookup(name, create_if_missing=False):
        return type("App", (), {"app_id": "app_1", "name": name})()


class FakeModal:
    Image = FakeImage
    Sandbox = FakeSandboxClass
    Volume = FakeVolumeClass
    Secret = FakeSecret
    App = FakeApp


class _FakeSyncEngine:
    baseline = object()

    def ensure_project_volume(self, *, project_id: str) -> dict:
        return {"volume_name": f"research-plugin-{project_id}"}

    def sync(self, *, project_id: str) -> None:
        return None


class ModalSandboxBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeSandbox.registry = {}
        FakeSandbox.tunnels_fail = False
        FakeSandboxClass.created = []
        FakeSandboxClass.by_name = {}
        FakeVolumeClass.instances = {}
        import os

        os.environ.setdefault("MODAL_TOKEN_ID", "tok")
        os.environ.setdefault("MODAL_TOKEN_SECRET", "sec")
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = ModalSandboxBackend(
            repo_root=Path(self.tmp.name),
            config=ModalConfig.from_env(),
            modal_module=FakeModal,
            sync_engine=_FakeSyncEngine(),
            start_poller=False,
        )

    def tearDown(self) -> None:
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
        self.assertEqual(kwargs["timeout"], 1234)
        self.assertEqual(kwargs["gpu"], "A100")
        self.assertIn(kwargs["workdir"], kwargs["volumes"])  # volume mounted at workdir
        self.assertIn("secrets", kwargs)
        # tags applied
        sandbox = FakeSandbox.registry[provisioned.sandbox_id]
        self.assertEqual(sandbox.tags["experiment_id"], "exp1")
        self.assertEqual(sandbox.tags["research_plugin_role"], "sandbox")

    def test_acquire_invokes_phase_and_created_callbacks(self) -> None:
        phases: list[str] = []
        created: list[tuple[str, str]] = []
        provisioned = self.backend.acquire(
            request=self._request(),
            on_phase=lambda p, _d: phases.append(p),
            on_created=lambda sid, name: created.append((sid, name)),
        )
        # the registry relies on these phases for visibility
        self.assertEqual(phases[0], "syncing")
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

    def test_find_sandbox_id(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        self.assertEqual(self.backend.find_sandbox_id(experiment_id="exp1"), provisioned.sandbox_id)
        self.assertIsNone(self.backend.find_sandbox_id(experiment_id="never"))

    def test_is_alive_and_terminate(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        self.assertTrue(self.backend.is_alive(sandbox_id=provisioned.sandbox_id))
        self.assertTrue(self.backend.terminate(sandbox_id=provisioned.sandbox_id))
        self.assertFalse(self.backend.is_alive(sandbox_id=provisioned.sandbox_id))

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

    def test_read_transcript_volume_fallback(self) -> None:
        provisioned = self.backend.acquire(request=self._request())
        # No live transcript; seed the committed Volume copy instead.
        volume = FakeVolumeClass.instances[provisioned.volume_name]
        rel = ".research_plugin_sessions/exp1/transcript.log"
        volume.files[rel] = b"committed transcript line\n"
        # Make the live read return empty so the fallback path is exercised.
        FakeSandbox.registry[provisioned.sandbox_id].transcript = ""
        text = self.backend.read_transcript(
            sandbox_id=provisioned.sandbox_id,
            experiment_id="exp1",
            volume_name=provisioned.volume_name,
            workdir=provisioned.workdir,
        )
        self.assertIn("committed transcript line", text)

    def test_health(self) -> None:
        self.assertTrue(self.backend.health()["ok"])


if __name__ == "__main__":
    unittest.main()
