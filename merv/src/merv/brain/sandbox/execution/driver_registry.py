"""Immutable lazy inventory for sandbox provider drivers.

Descriptors contain only metadata and an import string. Importing this module
does not import provider implementations, resolve credentials, or require an
optional provider SDK. A provider is loaded only when composition selects it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, cast

from ..sandbox_backend import (
    BackendUnavailableError,
    BackendValidationError,
    SandboxBackend,
)


ActivityHook = Callable[[str, dict[str, Any]], None]
DEFAULT_SANDBOX_DRIVER = "lambda_labs"
DRIVER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")


class SandboxDriverFactory(Protocol):
    """Uniform lazy-construction seam used by every provider."""

    def __call__(
        self,
        *,
        repo_root: Path,
        activity: ActivityHook | None = None,
    ) -> SandboxBackend: ...


def _normalized_name(value: str) -> str:
    return value.strip().lower()


@dataclass(frozen=True, slots=True)
class SandboxDriverDescriptor:
    """One lazily loaded provider implementation."""

    name: str
    factory_ref: str
    aliases: tuple[str, ...] = ()
    test_only: bool = False

    def __post_init__(self) -> None:
        canonical = _normalized_name(self.name)
        if canonical != self.name or not DRIVER_NAME_PATTERN.fullmatch(canonical):
            raise BackendValidationError(
                "sandbox driver name must match [a-z][a-z0-9_]*: "
                f"{self.name!r}"
            )
        module_name, separator, attribute = self.factory_ref.partition(":")
        if not separator or not module_name or not attribute or ":" in attribute:
            raise BackendValidationError(
                "sandbox driver factory_ref must be 'module.path:callable'"
            )
        normalized_aliases = tuple(_normalized_name(alias) for alias in self.aliases)
        if any(not alias for alias in normalized_aliases):
            raise BackendValidationError(
                f"sandbox driver {self.name} has an empty alias"
            )
        if any(
            not DRIVER_NAME_PATTERN.fullmatch(alias) for alias in normalized_aliases
        ):
            raise BackendValidationError(
                f"sandbox driver {self.name} has an invalid alias"
            )
        if self.name in normalized_aliases or len(set(normalized_aliases)) != len(
            normalized_aliases
        ):
            raise BackendValidationError(
                f"sandbox driver {self.name} has duplicate aliases"
            )
        if normalized_aliases != self.aliases:
            raise BackendValidationError(
                f"sandbox driver {self.name} aliases must be canonical lowercase"
            )

    def load_factory(self) -> SandboxDriverFactory:
        """Import the selected driver factory, leaving all others untouched."""
        module_name, _, attribute = self.factory_ref.partition(":")
        try:
            module = import_module(module_name)
            factory = getattr(module, attribute)
        except (AttributeError, ImportError) as exc:
            raise BackendUnavailableError(
                f"could not load sandbox driver {self.name}: {exc}"
            ) from exc
        if not callable(factory):
            raise BackendUnavailableError(
                f"sandbox driver factory is not callable: {self.factory_ref}"
            )
        return cast(SandboxDriverFactory, factory)


SANDBOX_DRIVER_DESCRIPTORS = (
    SandboxDriverDescriptor(
        name="lambda_labs",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.lambda_labs:"
            "build_lambda_labs_sandbox_backend"
        ),
        aliases=("lambda", "lambdalabs"),
    ),
    SandboxDriverDescriptor(
        name="thunder_compute",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.thunder_compute:"
            "build_thunder_compute_sandbox_backend"
        ),
        aliases=("thunder", "thundercompute"),
    ),
    SandboxDriverDescriptor(
        name="modal",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.modal:build_modal_sandbox_backend"
        ),
    ),
    SandboxDriverDescriptor(
        name="hyperstack",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.hyperstack:"
            "build_hyperstack_sandbox_backend"
        ),
    ),
    SandboxDriverDescriptor(
        name="digitalocean",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.digitalocean:"
            "build_digitalocean_sandbox_backend"
        ),
    ),
    SandboxDriverDescriptor(
        name="verda",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.verda:build_verda_sandbox_backend"
        ),
        aliases=("datacrunch",),
    ),
    SandboxDriverDescriptor(
        name="voltage_park",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.voltage_park:"
            "build_voltage_park_sandbox_backend"
        ),
        aliases=("voltagepark",),
    ),
    SandboxDriverDescriptor(
        name="tensordock",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.tensordock:"
            "build_tensordock_sandbox_backend"
        ),
    ),
    SandboxDriverDescriptor(
        name="fake",
        factory_ref=(
            "merv.brain.sandbox.execution.backends.fake:build_fake_sandbox_backend"
        ),
        test_only=True,
    ),
)

_DESCRIPTORS_BY_NAME: Mapping[str, SandboxDriverDescriptor] = MappingProxyType(
    {descriptor.name: descriptor for descriptor in SANDBOX_DRIVER_DESCRIPTORS}
)
SANDBOX_DRIVER_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        alias: descriptor.name
        for descriptor in SANDBOX_DRIVER_DESCRIPTORS
        for alias in descriptor.aliases
    }
)


def canonical_sandbox_driver_name(name: str) -> str:
    normalized = _normalized_name(name)
    return SANDBOX_DRIVER_ALIASES.get(normalized, normalized)


def sandbox_driver_descriptor(name: str) -> SandboxDriverDescriptor:
    canonical = canonical_sandbox_driver_name(name)
    try:
        return _DESCRIPTORS_BY_NAME[canonical]
    except KeyError as exc:
        raise BackendUnavailableError(
            f"unknown execution backend: {canonical}"
        ) from exc


def build_sandbox_driver(
    *,
    name: str,
    repo_root: Path,
    activity: ActivityHook | None = None,
) -> SandboxBackend:
    descriptor = sandbox_driver_descriptor(name)
    backend = descriptor.load_factory()(repo_root=repo_root, activity=activity)
    if not isinstance(backend, SandboxBackend):
        raise BackendValidationError(
            f"sandbox driver {descriptor.name} does not implement SandboxBackend"
        )
    if backend.capabilities.name != descriptor.name:
        raise BackendValidationError(
            f"sandbox driver {descriptor.name} built backend named "
            f"{backend.capabilities.name}"
        )
    return backend


def sandbox_driver_inventory() -> tuple[SandboxDriverDescriptor, ...]:
    """Return every driver without importing its implementation."""
    return SANDBOX_DRIVER_DESCRIPTORS


__all__ = [
    "ActivityHook",
    "DEFAULT_SANDBOX_DRIVER",
    "SANDBOX_DRIVER_ALIASES",
    "SANDBOX_DRIVER_DESCRIPTORS",
    "SandboxDriverDescriptor",
    "SandboxDriverFactory",
    "build_sandbox_driver",
    "canonical_sandbox_driver_name",
    "sandbox_driver_descriptor",
    "sandbox_driver_inventory",
]
