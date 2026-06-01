from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from backend.execution import (
    BackendUnavailableError,
    BackendValidationError,
    JobExecutionPolicy,
    build_execution_backend,
)
from backend.execution.backends.fake import FakeBackend
from backend.execution.backends.ray import RayExecutionBackend, RayRestJobClient
from backend.execution.backends.ray.backend import RAY_TO_LOCAL_STATUS
from backend.execution.backends.ray.clients import RaySdkJobClient
from backend.execution.errors import BackendPermissionError


class _PolicyBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "main.py").write_text("print('ok')\n")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _policy(self) -> JobExecutionPolicy:
        return JobExecutionPolicy(repo_root=self.tmp)


class CommandValidationTests(_PolicyBase):
    def test_accepts_allowed_executables(self) -> None:
        spec = self._policy().validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=None,
            env=None,
            backend_hints=None,
        )
        self.assertEqual(spec.command, "python src/main.py")
        self.assertEqual(spec.cwd, ".")

    def test_rejects_empty_command(self) -> None:
        with self.assertRaises(BackendValidationError):
            self._policy().validate(
                command="   ", cwd=".", expected_outputs=None, env=None, backend_hints=None
            )

    def test_rejects_disallowed_executable(self) -> None:
        with self.assertRaises(BackendPermissionError):
            self._policy().validate(
                command="rm -rf /",
                cwd=".",
                expected_outputs=None,
                env=None,
                backend_hints=None,
            )

    def test_rejects_shell_control_syntax(self) -> None:
        for bad in [
            "python a.py && python b.py",
            "python a.py | tee out",
            "python a.py > out.txt",
            "python a.py; echo hi",
            "python $(echo a.py)",
        ]:
            with self.subTest(bad=bad):
                with self.assertRaises(BackendPermissionError):
                    self._policy().validate(
                        command=bad,
                        cwd=".",
                        expected_outputs=None,
                        env=None,
                        backend_hints=None,
                    )


class PathValidationTests(_PolicyBase):
    def test_cwd_must_exist_and_be_dir(self) -> None:
        with self.assertRaises(BackendValidationError):
            self._policy().validate(
                command="python src/main.py",
                cwd="nope",
                expected_outputs=None,
                env=None,
                backend_hints=None,
            )
        with self.assertRaises(BackendValidationError):
            self._policy().validate(
                command="python src/main.py",
                cwd="src/main.py",
                expected_outputs=None,
                env=None,
                backend_hints=None,
            )

    def test_expected_outputs_must_stay_inside_repo(self) -> None:
        for bad in ["/etc/passwd", "../escape.txt", "src/../../oops.txt"]:
            with self.subTest(bad=bad):
                with self.assertRaises(BackendValidationError):
                    self._policy().validate(
                        command="python src/main.py",
                        cwd=".",
                        expected_outputs=[bad],
                        env=None,
                        backend_hints=None,
                    )

    def test_expected_outputs_are_normalised(self) -> None:
        spec = self._policy().validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=["results/out.json"],
            env=None,
            backend_hints={"x": 1},
        )
        self.assertEqual(tuple(spec.expected_outputs), ("results/out.json",))
        self.assertEqual(dict(spec.backend_hints), {"x": 1})


class EnvValidationTests(_PolicyBase):
    def test_rejects_sensitive_env_var_names(self) -> None:
        for bad_key in ["API_TOKEN", "DB_PASSWORD", "MY_SECRET_KEY", "PRIVATE_KEY"]:
            with self.subTest(key=bad_key):
                with self.assertRaises(BackendPermissionError):
                    self._policy().validate(
                        command="python src/main.py",
                        cwd=".",
                        expected_outputs=None,
                        env={bad_key: "x"},
                        backend_hints=None,
                    )

    def test_allows_safe_env_vars(self) -> None:
        spec = self._policy().validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=None,
            env={
                "PYTHONUNBUFFERED": "1",
                "EXPERIMENT_NAME": "baseline",
                "TOKENIZERS_PARALLELISM": "false",
            },
            backend_hints=None,
        )
        self.assertEqual(
            dict(spec.env),
            {
                "PYTHONUNBUFFERED": "1",
                "EXPERIMENT_NAME": "baseline",
                "TOKENIZERS_PARALLELISM": "false",
            },
        )


class FakeBackendTests(unittest.TestCase):
    def test_fake_backend_implements_contract(self) -> None:
        backend = FakeBackend()
        spec = JobExecutionPolicy(repo_root=Path.cwd()).validate(
            command="python -m unittest",
            cwd=".",
            expected_outputs=["result.json"],
            env=None,
            backend_hints={"mode": "test"},
        )
        runtime_job_id = backend.submit(spec=spec)
        self.assertEqual(backend.status(runtime_job_id=runtime_job_id).state, "queued")
        backend.set_status(runtime_job_id=runtime_job_id, state="succeeded")
        self.assertEqual(backend.status(runtime_job_id=runtime_job_id).state, "succeeded")


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class RayRestClientTests(unittest.TestCase):
    def test_submit_job_parses_response(self) -> None:
        client = RayRestJobClient(address="http://ray.example.com")
        with mock.patch(
            "backend.execution.backends.ray.clients.urlopen",
            return_value=FakeResponse(b'{"submission_id": "raysub_42"}'),
        ):
            runtime_job_id = client.submit_job(
                entrypoint="python a.py", runtime_env={}, metadata={}
            )
        self.assertEqual(runtime_job_id, "raysub_42")

    def test_get_job_logs_falls_back_to_message_when_logs_empty(self) -> None:
        client = RayRestJobClient(address="http://ray.example.com")
        responses = [
            FakeResponse(b'{"logs": ""}'),
            FakeResponse(b'{"message": "ray-info-message"}'),
        ]
        with mock.patch(
            "backend.execution.backends.ray.clients.urlopen",
            side_effect=responses,
        ):
            self.assertEqual(client.get_job_logs(runtime_job_id="x"), "ray-info-message")


class RayBackendTests(_PolicyBase):
    def test_known_statuses_map(self) -> None:
        self.assertEqual(RAY_TO_LOCAL_STATUS["PENDING"], "queued")
        self.assertEqual(RAY_TO_LOCAL_STATUS["RUNNING"], "running")
        self.assertEqual(RAY_TO_LOCAL_STATUS["STOPPED"], "cancelled")
        self.assertEqual(RAY_TO_LOCAL_STATUS["SUCCEEDED"], "succeeded")
        self.assertEqual(RAY_TO_LOCAL_STATUS["FAILED"], "failed")

    def test_rest_mode_rejects_local_working_dir_hint(self) -> None:
        backend = RayExecutionBackend(repo_root=self.tmp, client=RayRestJobClient(address="http://ray.example.com"))
        spec = self._policy().validate(
            command="python src/main.py",
            cwd=".",
            expected_outputs=None,
            env=None,
            backend_hints={"working_dir": "/tmp/local"},
        )
        with self.assertRaises(BackendValidationError):
            backend.submit(spec=spec)

    def test_factory_falls_back_to_rest_when_sdk_unavailable(self) -> None:
        with mock.patch.object(
            RaySdkJobClient,
            "__init__",
            side_effect=BackendUnavailableError("no sdk"),
        ):
            backend = build_execution_backend(repo_root=self.tmp, name="ray")
        self.assertEqual(backend.capabilities.name, "ray")
        self.assertFalse(backend.capabilities.supports_local_working_dir)


if __name__ == "__main__":
    unittest.main()
