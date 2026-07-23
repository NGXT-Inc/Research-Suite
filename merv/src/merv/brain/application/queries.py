"""Cross-component read models shared by delivery surfaces."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from merv.shared.artifact_roles import PROJECT_GRAPH_ROLES

from ..artifacts.facade import Artifacts
from ..research_core.facade import (
    ExperimentSummary,
    MAX_GRAPH_NODES,
    ResearchCore,
    graph_problems,
    preferred_associated_artifact,
)
from .ports.tracking import tracking_experiment_name
from .experiment_figure import build_experiment_figure
from .reflection_guidance import present_reflection_signal
from .reflections import present_reflection_overview, present_reflection_state

Record = dict[str, Any]
RecordQuery = Callable[..., Record]
RecordsQuery = Callable[..., list[Record]]


class TrackingOverview(Protocol):
    def health(self) -> dict[str, object]: ...

    def project_results_snapshot(
        self, *, project_id: str, experiment_ids: tuple[str, ...]
    ) -> tuple[dict[str, Record], list[Record], str]: ...

    def results_metrics(
        self, *, project_id: str, experiment_id: str, include_history: bool = True
    ) -> Record: ...


class ExperimentSummaries(Protocol):
    def __call__(self, *, project_id: str) -> list[ExperimentSummary]: ...


@dataclass(slots=True)
class MlflowOverviewQuery:
    """Join Research experiments to their external tracking read models."""

    experiments: ExperimentSummaries
    tracking: TrackingOverview

    def experiment_metrics(self, *, project_id: str, experiment_id: str) -> Record:
        return self.tracking.results_metrics(
            project_id=project_id, experiment_id=experiment_id
        )

    def __call__(self, *, project_id: str) -> Record:
        health = self.tracking.health()
        unreachable = health.get("reachable") is False
        experiments = self.experiments(project_id=project_id)
        snapshots, namespace, failure_hint = (
            ({}, [], "MLflow unreachable.")
            if unreachable
            else self.tracking.project_results_snapshot(
                project_id=project_id,
                experiment_ids=tuple(
                    str(item["id"]) for item in experiments if item.get("id")
                ),
            )
        )
        namespace_by_name = {
            str(entry.get("name") or ""): entry for entry in namespace
        }
        items: list[Record] = []
        for experiment in experiments:
            experiment_id = str(experiment.get("id") or "")
            if not experiment_id:
                continue
            mlflow_name = tracking_experiment_name(
                project_id=project_id, experiment_id=experiment_id
            )
            snapshot = snapshots.get(mlflow_name)
            namespace_entry = namespace_by_name.get(mlflow_name, {})
            dashboard_url = (
                str(namespace_entry.get("dashboard_experiment_url") or "")
                if snapshot is not None
                else ""
            )
            metrics: Record = {
                "experiment_id": experiment_id,
                "available": snapshot is not None,
                "source": "mlflow",
            }
            if snapshot is None:
                metrics["hint"] = failure_hint or (
                    "No MLflow runs found for this experiment yet."
                )
            else:
                metrics["experiments"] = [snapshot]
                if dashboard_url:
                    metrics["dashboard_experiment_url"] = dashboard_url
            items.append(
                {
                    "experiment_id": experiment_id,
                    "name": experiment.get("name") or experiment_id,
                    "status": experiment.get("status") or "",
                    "intent": experiment.get("intent") or "",
                    "mlflow_experiment_name": mlflow_name,
                    "dashboard_experiment_url": dashboard_url,
                    "metrics": metrics,
                }
            )
        expected_names = {str(item["mlflow_experiment_name"]) for item in items}
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
    experiments: ExperimentSummaries

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
        chosen = preferred_associated_artifact(
            artifacts=experiment.get("artifacts", []),
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
        return present_reflection_overview(
            self.research.reflection_overview(project_id=project_id)
        )

    def reflection(self, *, project_id: str, reflection_id: str) -> Record:
        return present_reflection_state(
            self.research.reflection_state(
                reflection_id=reflection_id, project_id=project_id
            )
        )

    def project(self, *, project_id: str) -> Record:
        selection = self.research.project_logic_graph_selection(project_id=project_id)
        return self._for_reflection(
            project_id=project_id,
            reflection=selection.get("reflection"),
            graph_artifact=selection.get("graph_artifact"),
            extra_base={"signal": present_reflection_signal(selection.get("signal"))},
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
        graph_artifact: Record | None = None,
        extra_base: Record | None = None,
    ) -> Record:
        base: Record = {"max_nodes": MAX_GRAPH_NODES, **(extra_base or {})}
        chosen = graph_artifact or (
            preferred_associated_artifact(
                artifacts=reflection.get("artifacts", []),
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
            "artifact_id": chosen.get("id"),
            "path": chosen.get("path"),
            "attempt_index": chosen.get("attempt_index"),
            "graph": graph,
            "problems": graph_problems(text),
            "ref_index": self._resolve_graph_refs(
                project_id=project_id, graph=graph
            ),
        }

    def _associated_text(self, artifact: Record) -> str | None:
        return self.artifacts.submitted_text_for_artifact(
            artifact_id=artifact.get("id")
        )

    def _resolve_graph_refs(
        self, *, project_id: str, graph: Record | None
    ) -> Record:
        refs = _refs_from_graph(graph)
        if not refs:
            return {}
        research = self.research.resolve_research_graph_refs(
            project_id=project_id, refs=tuple(refs)
        )
        resolved: Record = {}
        for ref in refs:
            if ref in research:
                resolved[ref] = research[ref]
                continue
            artifact = (
                self.artifacts.resolve_artifact_reference(
                    project_id=project_id, artifact_id=ref
                )
                if ref.startswith("art_")
                else None
            )
            resolved[ref] = artifact or {
                "type": "unknown",
                "resolved": False,
                "hint": (
                    "not a submitted artifact id; submit the file with "
                    "artifact.submit to make this ref resolvable"
                ),
            }
        return resolved


def _refs_from_graph(graph: Record | None) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, dict) or not isinstance(node.get("refs"), list):
            continue
        for ref in node["refs"]:
            if isinstance(ref, str) and ref.strip() and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs
