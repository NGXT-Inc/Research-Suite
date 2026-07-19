"""MCP reflection tool adapter over the reflection wave service."""

from __future__ import annotations

from typing import Any

from .reflections import ReflectionService


class ReflectionToolService:
    """Reflection tool names backed by reflection wave records."""

    def __init__(self, *, reflections: ReflectionService) -> None:
        self.reflections = reflections

    def create(
        self,
        *,
        project_id: str,
        title: str = "",
        lenses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.reflections.create(
            project_id=project_id,
            title=title,
            lenses=lenses or [],
        )

    def get(self, *, project_id: str, reflection_id: str) -> dict[str, Any]:
        return self.reflections.get_state(
            reflection_id=reflection_id,
            project_id=project_id,
        )

    def list(self, *, project_id: str) -> dict[str, Any]:
        state = self.reflections.list_reflections(project_id=project_id)
        reflections = state.get("reflections", [])
        return {
            "count": state.get("count", len(reflections)),
            "reflections": reflections,
        }

    def transition(
        self, *, project_id: str, reflection_id: str, transition: str
    ) -> dict[str, Any]:
        return self.reflections.transition(
            project_id=project_id,
            reflection_id=reflection_id,
            transition=transition,
        )
