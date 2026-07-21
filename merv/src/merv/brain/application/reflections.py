"""Application-facing reflection commands and response presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..research_core.facade import ResearchCore
from .gate_checklist import present_gate_checklist
from .reflection_guidance import post_publish_guidance, present_reflection_signal

Record = dict[str, Any]


@dataclass(slots=True)
class ReflectionCommands:
    """Delegate reflection policy to Research and present its semantic checklist."""

    reflections: ResearchCore

    def create(
        self,
        *,
        project_id: str,
        title: str = "",
        lenses: list[Record] | None = None,
    ) -> Record:
        return present_reflection_state(
            self.reflections.create_reflection(
                project_id=project_id, title=title, lenses=lenses or []
            )
        )

    def get(self, *, project_id: str, reflection_id: str) -> Record:
        return present_reflection_state(
            self.reflections.reflection_state(
                project_id=project_id, reflection_id=reflection_id
            )
        )

    def list(self, *, project_id: str) -> Record:
        result = self.reflections.list_reflections(project_id=project_id)
        return present_reflection_overview(
            {"count": result.get("count", len(result.get("reflections", []))), **result}
        )

    def transition(
        self, *, project_id: str, reflection_id: str, transition: str
    ) -> Record:
        return present_reflection_state(
            self.reflections.transition_reflection(
                project_id=project_id,
                reflection_id=reflection_id,
                transition=transition,
            )
        )


def present_reflection_state(state: Record) -> Record:
    result = dict(state)
    materialized = result.get("materialized_experiments")
    if result.get("status") == "published" and materialized:
        items = list(result.items())
        index = list(result).index("materialized_experiments") + 1
        items.insert(index, ("post_publish_guidance", post_publish_guidance(
            materialized_experiments=materialized
        )))
        result = dict(items)
    checklist = result.get("gate_checklist")
    if not isinstance(checklist, dict):
        return result
    result["gate_checklist"] = present_gate_checklist(checklist)
    return result


def present_reflection_overview(overview: Record) -> Record:
    state_keys = {"current", "open_reflection", "latest_published"}
    result = dict(overview)
    result["reflections"] = [
        present_reflection_state(item) for item in result.get("reflections", [])
    ]
    for key in state_keys:
        if isinstance(result.get(key), dict):
            result[key] = present_reflection_state(result[key])
    if isinstance(result.get("signal"), dict):
        result["signal"] = present_reflection_signal(result["signal"])
    return result


__all__ = ["ReflectionCommands", "present_reflection_overview", "present_reflection_state"]
