"""Ports used by reflection tool adapters."""

from __future__ import annotations

from typing import Any, Protocol


class ReflectionWaveStore(Protocol):
    """Internal reflection-wave store operations exposed through tool aliases."""

    def create(
        self,
        *,
        project_id: str | None = None,
        title: str = "",
        lenses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        ...

    def get_state(
        self,
        *,
        synthesis_id: str,
        project_id: str | None = None,
        conn: Any | None = None,
    ) -> dict[str, Any]:
        ...

    def list_syntheses(self, *, project_id: str | None = None) -> dict[str, Any]:
        ...

    def transition(
        self,
        *,
        project_id: str | None,
        synthesis_id: str,
        transition: str,
    ) -> dict[str, Any]:
        ...
