"""Typed command and query values accepted by the Sandbox facade."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GetSandboxQuery:
    experiment_id: str | None = None
    project_id: str | None = None
    tenant_id: str | None = None
    sandbox_uid: str | None = None
    include_data_plane_enrichment: bool = True


@dataclass(frozen=True, slots=True)
class ListSandboxesQuery:
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxOptionsQuery:
    project_id: str | None = None
    gpu: str | None = None
    region: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxTerminalQuery:
    experiment_id: str | None = None
    project_id: str | None = None
    sandbox_uid: str | None = None
    tail: int | None = None
    since: int | None = None


@dataclass(frozen=True, slots=True)
class SandboxRunsQuery:
    experiment_id: str | None = None
    project_id: str | None = None
    tenant_id: str | None = None
    sandbox_uid: str | None = None
    wait_seconds: int = 0


@dataclass(frozen=True, slots=True)
class RequestSandboxCommand:
    experiment_id: str | None = None
    project_id: str | None = None
    gpu: str | None = None
    cpu: float | None = None
    memory: int | None = None
    time_limit: int | None = None
    instance_type: str | None = None
    region: str | None = None
    provider: str | None = None
    public_key: str | None = None
    public_key_override: str | None = None
    include_data_plane_enrichment: bool = True
    additional: bool = False
    sandbox_uid: str | None = None


@dataclass(frozen=True, slots=True)
class AttachSandboxCommand:
    experiment_id: str
    sandbox_uid: str
    project_id: str | None = None
    include_data_plane_enrichment: bool = True
    public_key_override: str | None = None


@dataclass(frozen=True, slots=True)
class ExtendSandboxCommand:
    experiment_id: str | None = None
    project_id: str | None = None
    tenant_id: str | None = None
    sandbox_uid: str | None = None
    seconds: int = 1800


@dataclass(frozen=True, slots=True)
class ReleaseSandboxCommand:
    experiment_id: str | None = None
    project_id: str | None = None
    sandbox_uid: str | None = None
    confirm_retained: bool = False


__all__ = [
    "AttachSandboxCommand",
    "ExtendSandboxCommand",
    "GetSandboxQuery",
    "ListSandboxesQuery",
    "ReleaseSandboxCommand",
    "RequestSandboxCommand",
    "SandboxOptionsQuery",
    "SandboxRunsQuery",
    "SandboxTerminalQuery",
]
