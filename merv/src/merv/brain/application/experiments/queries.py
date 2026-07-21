"""Application-owned experiment collection views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...research_core.facade import ExperimentState, ResearchCore
from ..ports.storage import ProducedObject, ProducedObjectCatalog
from .presentation import rich_experiment_state, slim_experiment_state


@dataclass(slots=True)
class ExperimentCollectionQuery:
    """Batch-compose Research states with produced-object facts."""

    research: ResearchCore
    objects: ProducedObjectCatalog

    def rich(self, *, project_id: str | None = None) -> list[ExperimentState]:
        states, by_experiment = self._read(project_id=project_id)
        return [
            rich_experiment_state(
                state,
                storage_objects=by_experiment.get(str(state.get("id") or ""), []),
            )
            for state in states
        ]

    def agent(self, *, project_id: str | None = None) -> dict[str, Any]:
        states, by_experiment = self._read(project_id=project_id)
        return {
            "experiments": [
                slim_experiment_state(
                    state,
                    storage_objects=by_experiment.get(
                        str(state.get("id") or ""), []
                    ),
                )
                for state in states
            ]
        }

    def _read(
        self, *, project_id: str | None
    ) -> tuple[list[ExperimentState], dict[str, list[ProducedObject]]]:
        states = self.research.project_experiments(project_id=project_id)
        ids = tuple(str(state.get("id") or "") for state in states if state.get("id"))
        if not ids:
            return states, {}
        resolved_project_id = str(states[0].get("project_id") or project_id or "")
        return states, self.objects.by_experiment(
            project_id=resolved_project_id, experiment_ids=ids
        )


__all__ = ["ExperimentCollectionQuery"]
