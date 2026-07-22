"""Reusable offline assertions for registered sandbox provider drivers.

This module is deliberately not named ``test_*``. Provider tests can reuse the
same harness with injected clients/transports without importing another test
module or touching a real cloud API.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable
from unittest import TestCase

from merv.brain.sandbox.execution.driver_registry import SandboxDriverDescriptor
from merv.brain.sandbox.sandbox_backend import (
    BackendUnavailableError,
    SandboxBackend,
    SandboxDriver,
    SandboxRequest,
)


DRIVER_METHODS = (
    "capabilities_for",
    "hardware_catalog",
    "acquire",
    "is_alive",
    "refresh_ssh_endpoint",
    "terminate",
)
MANAGEMENT_METHODS = (
    "read_transcript",
    "sample_metrics",
    "read_runs",
    "write_secrets",
)
COMPATIBILITY_METHODS = (
    "sandbox_environment",
    "health",
    "find_sandbox_id",
    "sandbox_secrets",
    "shutdown",
)


@dataclass(frozen=True)
class OfflineDriverFixture:
    """Hooks that put an injected driver into observable offline states."""

    descriptor: SandboxDriverDescriptor
    backend: SandboxBackend
    request: SandboxRequest
    set_transcript: Callable[[str, str], None]
    set_metrics: Callable[[str, dict], None]
    set_runs: Callable[[str, str], None]
    move_endpoint: Callable[[str, str, int], None] | None = None
    expected_refreshed_endpoint: tuple[str, int] | None = None
    management_key_path: str = "/offline/management-key"
    set_outage: Callable[[bool], None] | None = None


def assert_driver_surface(
    case: TestCase,
    *,
    descriptor: SandboxDriverDescriptor,
    backend: SandboxBackend,
) -> None:
    """Assert metadata identity and every stable contract operation."""
    case.assertIsInstance(backend, SandboxDriver)
    case.assertEqual(backend.capabilities.name, descriptor.name)
    case.assertIs(
        backend.capabilities_for(provider=descriptor.name), backend.capabilities
    )
    for method in DRIVER_METHODS:
        case.assertTrue(
            callable(getattr(backend, method, None)),
            f"{descriptor.name} is missing {method}",
        )
    for method in MANAGEMENT_METHODS:
        case.assertTrue(
            callable(getattr(backend, method, None)),
            f"{descriptor.name} backend is missing {method}",
        )
    for method in COMPATIBILITY_METHODS:
        case.assertTrue(
            callable(getattr(backend, method, None)),
            f"{descriptor.name} compatibility backend is missing {method}",
        )


def assert_catalog_envelope(
    case: TestCase,
    *,
    descriptor: SandboxDriverDescriptor,
    backend: SandboxBackend,
) -> dict:
    """Assert provider-neutral catalog fields without imposing a VM shape."""
    catalog = backend.hardware_catalog()
    case.assertIsInstance(catalog, dict)
    assert isinstance(catalog, dict)
    case.assertEqual(catalog.get("provider"), descriptor.name)
    case.assertEqual(
        bool(catalog.get("selection_required")),
        backend.capabilities.requires_hardware_selection,
    )
    case.assertTrue(str(catalog.get("select_with") or ""))
    case.assertTrue(str(catalog.get("reason") or ""))
    return catalog


def exercise_offline_driver(case: TestCase, fixture: OfflineDriverFixture) -> None:
    """Exercise lifecycle and management semantics against injected fakes."""
    backend = fixture.backend
    phases: list[tuple[str, str]] = []
    created: list[tuple[str, str]] = []
    provisioned = backend.acquire(
        request=fixture.request,
        on_phase=lambda phase, detail: phases.append((phase, detail)),
        on_created=lambda sandbox_id, name: created.append((sandbox_id, name)),
    )
    case.assertTrue(provisioned.sandbox_id)
    case.assertTrue(provisioned.ssh_host)
    case.assertGreaterEqual(provisioned.ssh_port, 1)
    case.assertLessEqual(provisioned.ssh_port, 65535)
    case.assertTrue(provisioned.ssh_user)
    case.assertTrue(provisioned.workdir)
    case.assertEqual([item[0] for item in created], [provisioned.sandbox_id])
    case.assertIn("creating", [phase for phase, _ in phases])
    case.assertIn("connecting", [phase for phase, _ in phases])
    case.assertTrue(backend.is_alive(sandbox_id=provisioned.sandbox_id))

    if fixture.move_endpoint is not None:
        fixture.move_endpoint(provisioned.sandbox_id, "moved.sandbox.test", 2222)
    refreshed = backend.refresh_ssh_endpoint(sandbox_id=provisioned.sandbox_id)
    case.assertEqual(refreshed, fixture.expected_refreshed_endpoint)

    fixture.set_transcript(fixture.request.experiment_id, "alpha-omega")
    transcript = backend.read_transcript(
        sandbox_id=provisioned.sandbox_id,
        experiment_id=fixture.request.experiment_id,
        volume_name=provisioned.volume_name,
        workdir=provisioned.workdir,
        tail=5,
        ssh_host=provisioned.ssh_host,
        ssh_port=provisioned.ssh_port,
        ssh_user=provisioned.ssh_user,
        key_path=fixture.management_key_path,
    )
    case.assertEqual(transcript.data, b"omega")
    case.assertEqual(transcript.total_bytes, len(b"alpha-omega"))

    fixture.set_metrics(provisioned.sandbox_id, {"cpu_percent": 12.5})
    case.assertEqual(
        backend.sample_metrics(
            sandbox_id=provisioned.sandbox_id,
            ssh_host=provisioned.ssh_host,
            ssh_port=provisioned.ssh_port,
            ssh_user=provisioned.ssh_user,
            key_path=fixture.management_key_path,
        ),
        {"cpu_percent": 12.5},
    )
    fixture.set_runs(
        provisioned.sandbox_id,
        '===MERV_RUN train\n{"label":"train","command":"python train.py"}\n'
        "===EXIT 0\n===FIN 2026-07-19T12:00:00Z\n",
    )
    runs = backend.read_runs(
        sandbox_id=provisioned.sandbox_id,
        workdir=provisioned.workdir,
        ssh_host=provisioned.ssh_host,
        ssh_port=provisioned.ssh_port,
        ssh_user=provisioned.ssh_user,
        key_path=fixture.management_key_path,
    )
    case.assertIsNotNone(runs)
    assert runs is not None
    case.assertEqual(runs[0]["label"], "train")
    case.assertEqual(runs[0]["exit_code"], 0)
    case.assertIsInstance(
        backend.write_secrets(
            sandbox_id=provisioned.sandbox_id,
            secrets={"OFFLINE_TEST_TOKEN": "not-a-real-secret"},
            ssh_host=provisioned.ssh_host,
            ssh_port=provisioned.ssh_port,
            key_path=fixture.management_key_path,
        ),
        bool,
    )

    case.assertTrue(backend.terminate(sandbox_id=provisioned.sandbox_id))
    case.assertFalse(backend.is_alive(sandbox_id=provisioned.sandbox_id))
    case.assertIsNone(
        backend.refresh_ssh_endpoint(sandbox_id=provisioned.sandbox_id)
    )
    case.assertIsNone(
        backend.sample_metrics(
            sandbox_id=provisioned.sandbox_id,
            ssh_host=provisioned.ssh_host,
            ssh_port=provisioned.ssh_port,
            ssh_user=provisioned.ssh_user,
            key_path=fixture.management_key_path,
        )
    )
    case.assertIsNone(
        backend.read_runs(
            sandbox_id=provisioned.sandbox_id,
            workdir=provisioned.workdir,
            ssh_host=provisioned.ssh_host,
            ssh_port=provisioned.ssh_port,
            ssh_user=provisioned.ssh_user,
            key_path=fixture.management_key_path,
        )
    )

    cancelled: list[str] = []

    def cancel_after_create(sandbox_id: str, _name: str) -> None:
        cancelled.append(sandbox_id)
        raise RuntimeError("offline cancellation")

    with case.assertRaisesRegex(RuntimeError, "offline cancellation"):
        backend.acquire(
            request=replace(fixture.request, sandbox_uid="cancelled-offline"),
            on_created=cancel_after_create,
        )
    case.assertEqual(len(cancelled), 1)
    case.assertFalse(backend.is_alive(sandbox_id=cancelled[0]))

    if fixture.set_outage is not None:
        fixture.set_outage(True)
        try:
            with case.assertRaises(BackendUnavailableError):
                backend.is_alive(sandbox_id="provider-outage-probe")
        finally:
            fixture.set_outage(False)


__all__ = [
    "OfflineDriverFixture",
    "assert_catalog_envelope",
    "assert_driver_surface",
    "exercise_offline_driver",
]
