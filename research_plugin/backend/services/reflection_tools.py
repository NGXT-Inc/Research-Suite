"""MCP reflection tool adapter over the internal synthesis service."""

from __future__ import annotations

from typing import Any

from ..domain.reflection_projection import external_reflection_state
from ..ports.reflection_waves import ReflectionWaveStore


class ReflectionToolService:
    """External reflection tool names backed by internal synthesis records."""

    def __init__(self, *, syntheses: ReflectionWaveStore) -> None:
        self.syntheses = syntheses

    def create(
        self,
        *,
        project_id: str,
        title: str = "",
        lenses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return external_reflection_state(
            self.syntheses.create(
                project_id=project_id,
                title=title,
                lenses=lenses or [],
            )
        )

    def get(self, *, project_id: str, reflection_id: str) -> dict[str, Any]:
        return external_reflection_state(
            self.syntheses.get_state(
                synthesis_id=reflection_id,
                project_id=project_id,
            )
        )

    def list(self, *, project_id: str) -> dict[str, Any]:
        state = self.syntheses.list_syntheses(project_id=project_id)
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
        internal_transition = (
            "submit_synthesis"
            if transition == "submit_reflection_artifacts"
            else transition
        )
        return external_reflection_state(
            self.syntheses.transition(
                project_id=project_id,
                synthesis_id=reflection_id,
                transition=internal_transition,
            )
        )
