"""Cross-component read models shared by delivery surfaces."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from merv.shared.artifact_roles import PROJECT_GRAPH_ROLES

from ..artifacts.facade import Artifacts, build_experiment_figure, preferred_associated_resource
from ..research_core.facade import MAX_GRAPH_NODES, ResearchCore, graph_problems
from .experiments.tracking_policy import mlflow_experiment_name

Record = dict[str, Any]
RecordQuery = Callable[..., Record]
RecordsQuery = Callable[..., list[Record]]


class TrackingOverview(Protocol):
    def health(self) -> dict[str, object]: ...

    def results_metrics(
        self, *, project_id: str, experiment_id: str, include_history: bool = True
    ) -> Record: ...

    def namespace_experiments(self, *, project_id: str) -> list[Record]: ...


@dataclass(slots=True)
class MlflowOverviewQuery:
    """Join Research experiments to their external tracking read models."""

    experiments: RecordQuery
    tracking: TrackingOverview

    def experiment_metrics(self, *, project_id: str, experiment_id: str) -> Record:
        return self.tracking.results_metrics(
            project_id=project_id, experiment_id=experiment_id
        )

    def __call__(self, *, project_id: str) -> Record:
        health = self.tracking.health()
        unreachable = health.get("reachable") is False
        items: list[Record] = []
        for experiment in self.experiments(project_id=project_id)["experiments"]:
            experiment_id = str(experiment.get("id") or "")
            if not experiment_id:
                continue
            metrics = (
                {
                    "experiment_id": experiment_id,
                    "available": False,
                    "source": "mlflow",
                    "hint": "MLflow unreachable.",
                }
                if unreachable
                else self.tracking.results_metrics(
                    project_id=project_id,
                    experiment_id=experiment_id,
                    include_history=False,
                )
            )
            items.append(
                {
                    "experiment_id": experiment_id,
                    "name": experiment.get("name") or experiment_id,
                    "status": experiment.get("status") or "",
                    "intent": experiment.get("intent") or "",
                    "mlflow_experiment_name": mlflow_experiment_name(
                        project_id=project_id, experiment_id=experiment_id
                    ),
                    "dashboard_experiment_url": metrics.get("dashboard_experiment_url", ""),
                    "metrics": metrics,
                }
            )
        expected_names = {str(item["mlflow_experiment_name"]) for item in items}
        namespace = [] if unreachable else self.tracking.namespace_experiments(
            project_id=project_id
        )
        return {
            "mlflow": health,
            "experiments": items,
            "unmapped_mlflow_experiments": [
                experiment
                for experiment in namespace
                if str(experiment.get("name") or "") not in expected_names
            ],
        }


@dataclass(slots=True)
class ExperimentFigureQuery:
    """Gather component facts and build one derived experiment figure."""

    experiment_state: RecordQuery
    review_snapshot: RecordQuery
    open_reviews: RecordsQuery
    sandbox_row: Callable[..., Record | None]
    sandbox_view: RecordQuery
    sandbox_status_active: Callable[[str], bool]

    def __call__(self, *, project_id: str, experiment_id: str) -> Record:
        experiment = self.experiment_state(
            experiment_id=experiment_id, project_id=project_id
        )
        review_attempts = {}
        for review in experiment.get("reviews", []):
            snapshot = self.review_snapshot(
                snapshot_id=str(review.get("target_snapshot_id") or "")
            )
            review_attempts[str(review.get("id"))] = int(snapshot.get("attempt_index") or 0)
        row = self.sandbox_row(experiment_id=experiment_id, project_id=project_id)
        sandbox = self.sandbox_view(row=row) if row is not None else None
        return build_experiment_figure(
            experiment=experiment,
            review_attempts=review_attempts,
            open_review_requests=self.open_reviews(
                project_id=project_id, experiment_id=experiment_id
            ),
            sandbox=sandbox,
            sandbox_active=bool(
                sandbox
                and self.sandbox_status_active(str(sandbox.get("status") or ""))
            ),
        )


@dataclass(slots=True)
class TenantCountersQuery:
    """Join Kernel audit counts to Sandbox generation accounting."""

    event_count: Callable[..., int]
    generation_counters: RecordQuery

    def __call__(self, *, tenant_id: str) -> Record:
        return {
            "tenant_id": tenant_id,
            "tool_calls": self.event_count(tenant_id=tenant_id),
            **self.generation_counters(tenant_id=tenant_id),
        }


@dataclass(slots=True)
class ComputeCostQuery:
    """Hydrate the Sandbox spend ledger with Research experiment names."""

    project_spend: RecordQuery
    experiments: RecordsQuery

    def __call__(self, *, project_id: str) -> Record:
        spend = self.project_spend(project_id=project_id)
        names = {
            str(experiment.get("id") or ""): str(experiment.get("name") or "")
            for experiment in self.experiments(project_id=project_id)
        }
        for entry in spend["by_experiment"]:
            entry["experiment_name"] = names.get(entry["experiment_id"], "")
        return spend


@dataclass(slots=True)
class LogicGraphQuery:
    """Build the common logic-graph view from Research and Artifacts facts."""

    research: ResearchCore
    artifacts: Artifacts

    def experiment(self, *, project_id: str, experiment_id: str) -> Record:
        experiment = self.research.experiment_state(
            experiment_id=experiment_id, project_id=project_id
        )
        attempt = experiment.get("attempt_index")
        chosen = preferred_associated_resource(
            resources=experiment.get("resources", []),
            attempt=attempt,
            roles=("graph",),
        )
        base = {
            "experiment_id": experiment_id,
            "max_nodes": MAX_GRAPH_NODES,
            "experiment_status": experiment.get("status"),
            "attempt_index": attempt,
        }
        if chosen is None:
            return {**base, "available": False, "graph": None, "problems": []}
        text = self._associated_text(chosen)
        if text is None:
            return {
                **base,
                "available": False,
                "graph": None,
                "problems": [
                    "graph has no submitted content — re-associate it (role 'graph')"
                ],
                "path": chosen.get("path"),
            }
        return self._payload(
            base=base, chosen=chosen, text=text, project_id=project_id
        )

    def reflections(self, *, project_id: str) -> Record:
        return self.research.reflection_overview(project_id=project_id)

    def reflection(self, *, project_id: str, reflection_id: str) -> Record:
        return self.research.reflection_state(
            reflection_id=reflection_id, project_id=project_id
        )

    def project(self, *, project_id: str) -> Record:
        selection = self.research.project_logic_graph_selection(project_id=project_id)
        return self._for_reflection(
            project_id=project_id,
            reflection=selection.get("reflection"),
            graph_resource=selection.get("graph_resource"),
            extra_base={"signal": selection.get("signal")},
        )

    def reflection_graph(self, *, project_id: str, reflection_id: str) -> Record:
        return self._for_reflection(
            project_id=project_id,
            reflection=self.reflection(
                project_id=project_id, reflection_id=reflection_id
            ),
        )

    def _for_reflection(
        self,
        *,
        project_id: str,
        reflection: Record | None,
        graph_resource: Record | None = None,
        extra_base: Record | None = None,
    ) -> Record:
        base: Record = {"max_nodes": MAX_GRAPH_NODES, **(extra_base or {})}
        chosen = graph_resource or (
            preferred_associated_resource(
                resources=reflection.get("resources", []),
                attempt=reflection.get("attempt_index"),
                roles=PROJECT_GRAPH_ROLES,
            )
            if reflection
            else None
        )
        if reflection is None or chosen is None:
            return {
                **base,
                "available": False,
                "reflection": None,
                "graph": None,
                "problems": [],
            }
        base["reflection"] = {
            "id": reflection.get("id"),
            "title": reflection.get("title"),
            "status": reflection.get("status"),
            "attempt_index": reflection.get("attempt_index"),
            "published_at": reflection.get("published_at"),
        }
        text = self._associated_text(chosen)
        if text is None:
            return {
                **base,
                "available": False,
                "graph": None,
                "problems": [
                    "graph has no submitted content — re-associate it "
                    "(role 'project_graph')"
                ],
                "path": chosen.get("path"),
            }
        return self._payload(
            base=base, chosen=chosen, text=text, project_id=project_id
        )

    def _payload(
        self,
        *,
        base: Record,
        chosen: Record,
        text: str,
        project_id: str,
    ) -> Record:
        graph: Record | None = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                graph = parsed
        except json.JSONDecodeError:
            pass
        return {
            **base,
            "available": True,
            "resource_id": chosen.get("id"),
            "path": chosen.get("path"),
            "association_attempt_index": chosen.get("association_attempt_index"),
            "graph": graph,
            "problems": graph_problems(text),
            "ref_index": self.research.resolve_graph_refs(
                project_id=project_id, graph=graph
            ),
        }

    def _associated_text(self, resource: Record) -> str | None:
        return self.artifacts.submitted_text_for_version(
            version_id=resource.get("association_version_id")
        )
