"""Stable read-only Sandbox entrypoint for application queries."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..kernel.state.store import BaseStateStore
from .sandboxes import SandboxFacade


@runtime_checkable
class SandboxReads(Protocol):
    def for_experiment(
        self, *, project_id: str, experiment_id: str
    ) -> list[dict[str, Any]]: ...

    def for_project(self, *, project_id: str) -> list[dict[str, Any]]: ...


class _SandboxRowReader(Protocol):
    def sandboxes_for_experiment(
        self, *, conn: Any, experiment_id: str
    ) -> list[dict[str, Any]]: ...

    def sandboxes_for_project(
        self, *, conn: Any, project_id: str
    ) -> list[dict[str, Any]]: ...


class SandboxReadFacade:
    """Hide Sandbox storage and row projection behind two stable reads."""

    __slots__ = ("_store", "_reader")

    def __init__(self, *, store: BaseStateStore, reader: _SandboxRowReader) -> None:
        self._store = store
        self._reader = reader

    def for_experiment(
        self, *, project_id: str, experiment_id: str
    ) -> list[dict[str, Any]]:
        with self._store.transaction() as conn:
            self._store.require_project_id(conn=conn, project_id=project_id)
            return self._reader.sandboxes_for_experiment(
                conn=conn, experiment_id=experiment_id
            )

    def for_project(self, *, project_id: str) -> list[dict[str, Any]]:
        with self._store.transaction() as conn:
            self._store.require_project_id(conn=conn, project_id=project_id)
            return self._reader.sandboxes_for_project(conn=conn, project_id=project_id)


__all__ = ["SandboxFacade", "SandboxReadFacade", "SandboxReads"]
