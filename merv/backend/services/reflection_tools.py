"""MCP reflection tool adapter over the reflection wave service."""

from __future__ import annotations

from typing import Any

from ..domain.reflection_projection import (
    external_reflection_state,
    internal_synthesis_transition,
)
from .reflections import ReflectionService


class ReflectionToolService:
    """External reflection tool names backed by reflection wave records."""

    def __init__(self, *, reflections: ReflectionService) -> None:
        self.reflections = reflections

    def create(
        self,
        *,
        project_id: str,
        title: str = "",
        lenses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return external_reflection_state(
            self.reflections.create(
                project_id=project_id,
                title=title,
                lenses=lenses or [],
            )
        )

    def get(self, *, project_id: str, reflection_id: str) -> dict[str, Any]:
        return external_reflection_state(
            self.reflections.get_state(
                synthesis_id=reflection_id,
                project_id=project_id,
            )
        )

    def list(self, *, project_id: str) -> dict[str, Any]:
        state = self.reflections.list_reflections(project_id=project_id)
        return {
            "count": state.get("count", len(state.get("syntheses", []))),
            "reflections": [
                external_reflection_state(item)
                for item in state.get("syntheses", [])
            ],
        }

    def transition(
        self, *, project_id: str, reflection_id: str, transition: str
    ) -> dict[str, Any]:
        internal_transition = internal_synthesis_transition(transition)
        return external_reflection_state(
            self.reflections.transition(
                project_id=project_id,
                synthesis_id=reflection_id,
                transition=internal_transition,
            )
        )
