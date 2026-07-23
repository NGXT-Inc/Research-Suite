"""Application-owned workflow and project dashboard read models."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from merv.shared.artifact_roles import PROJECT_GRAPH_ROLES

from ..research_core.facade import (
    EXPERIMENT_ACTIVE_PROCESS_STATUSES,
    EXPERIMENT_TERMINAL_STATUSES,
    ResearchSnapshot,
    ResearchSnapshots,
)
from .experiments.presentation import project_fields, project_rows, rich_experiment_state
from .ports.sandbox import SandboxReads
from .ports.storage import ProducedObjectCatalog
from .reflection_guidance import literature_hint
from .status_guidance import (
    StatusGuidancePolicy,
    _SLIM_ARTIFACT_FIELDS,
    _SLIM_REVIEW_FIELDS,
)

Record = dict[str, Any]
RecordQuery = Callable[..., Record]

_EXPERIMENT_PRIORITY = {
    "running": 0,
    "experiment_review": 1,
    "design_review": 2,
    "ready_to_run": 3,
    "planned": 4,
}
_PROCESS_PRIORITY = {"running": 0, "provisioning": 1}
_STATUS_EXPERIMENT_FIELDS = ("id", "name", "intent", "status", "attempt_index")
_SANDBOX_SUMMARY_FIELDS = (
    "sandbox_id",
    "status",
    "gpu",
    "cpu",
    "memory",
    "ssh_host",
    "ssh_port",
    "ssh_user",
    "workdir",
    "sandbox_data_dir",
    "expires_at",
)


@dataclass
class StatusAndNextQuery:
    """Join Research snapshots to Sandbox reads, then apply pure policy."""

    snapshots: ResearchSnapshots
    sandboxes: SandboxReads
    policy: StatusGuidancePolicy
    objects: ProducedObjectCatalog

    def status_and_next(
        self, *, project_id: str | None = None, experiment_id: str | None = None
    ) -> Record:
        snapshot = self.snapshots.read(
            project_id=project_id, experiment_id=experiment_id
        )
        selected = snapshot.selected_experiment
        sandbox_rows = (
            self.sandboxes.for_experiment(
                project_id=snapshot.project_id, experiment_id=str(selected["id"])
            )
            if selected is not None
            else []
        )
        experiment = (
            self._enrich(
                project_id=snapshot.project_id, experiments=[selected]
            )[0]
            if selected is not None
            else None
        )
        return self._status(
            snapshot=snapshot, experiment=experiment, sandboxes=sandbox_rows
        )

    def status_and_next_agent(
        self, *, project_id: str | None = None, experiment_id: str | None = None
    ) -> Record:
        return _slim_status(
            self.status_and_next(project_id=project_id, experiment_id=experiment_id)
        )

    def project_models(
        self, *, snapshot: ResearchSnapshot, sandboxes: list[Record]
    ) -> tuple[Record, Record, list[Record]]:
        experiments = self._enrich(
            project_id=snapshot.project_id,
            experiments=snapshot.experiment_states,
        )
        by_id = {str(item["id"]): item for item in experiments}
        selected = (
            by_id.get(str(snapshot.selected_experiment["id"]))
            if snapshot.selected_experiment is not None
            else None
        )
        selected_sandboxes = []
        if selected is not None:
            selected_id = str(selected["id"])
            selected_sandboxes = [
                {**sandbox, "experiment_id": selected_id}
                for sandbox in sandboxes
                if selected_id in (sandbox.get("active_experiment_ids") or [])
            ]
        return (
            self._status(
                snapshot=snapshot,
                experiment=selected,
                sandboxes=selected_sandboxes,
            ),
            self._active_work(
                snapshot=snapshot,
                experiments=experiments,
                sandboxes=sandboxes,
            ),
            experiments,
        )

    def _status(
        self,
        *,
        snapshot: ResearchSnapshot,
        experiment: Record | None,
        sandboxes: list[Record],
    ) -> Record:
        workflow = (
            self.policy.experiment(
                experiment=experiment,
                sandboxes=sandboxes,
                evaluation=snapshot.gate_evaluations[str(experiment["id"])],
            )
            if experiment is not None
            else self.policy.project_setup()
        )
        idle = all(
            str(row["status"]) in EXPERIMENT_TERMINAL_STATUSES
            for row in snapshot.experiments
        )
        reflection = self.policy.project_reflection(
            open_wave=snapshot.open_reflection,
            evaluation=(
                None
                if snapshot.open_reflection is None
                else snapshot.gate_evaluations[str(snapshot.open_reflection["id"])]
            ),
            signal=snapshot.reflection_signal,
            idle=idle,
        )
        if snapshot.requested_experiment_id is None and idle:
            workflow = (
                self.policy.reflection_workflow_takeover(reflection=reflection)
                or workflow
            )
        elif (
            snapshot.requested_experiment_id is None
            and experiment is not None
            and str(experiment.get("status")) in EXPERIMENT_TERMINAL_STATUSES
        ):
            workflow = self.policy.live_experiments_takeover(
                exp_rows=snapshot.experiments, reflection=reflection
            )
        result = {
            "project": {
                **snapshot.project,
                "active_claims": snapshot.claims,
                "active_experiments": project_rows(
                    snapshot.experiments, _STATUS_EXPERIMENT_FIELDS
                ),
            },
            "experiment": experiment,
            "sandboxes": sandboxes,
            "workflow": workflow,
        }
        if reflection is not None:
            result["project_reflection"] = reflection
        hint = literature_hint(signal=snapshot.literature_signal)
        if hint is not None:
            result["litreview"] = {
                **snapshot.literature_signal,
                "hint": hint,
            }
        return result

    def _active_work(
        self,
        *,
        snapshot: ResearchSnapshot,
        experiments: list[Record],
        sandboxes: list[Record],
    ) -> Record:
        by_id = {str(item["id"]): item for item in experiments}
        processes = _sort_active(
            [
                _process_view(
                    sandbox=sandbox,
                    experiment=by_id.get(
                        str((sandbox.get("active_experiment_ids") or [""])[0])
                    ),
                    experiments=[
                        by_id[experiment_id]
                        for experiment_id in sandbox.get("active_experiment_ids") or []
                        if experiment_id in by_id
                    ],
                )
                for sandbox in sandboxes
                if sandbox.get("status") in EXPERIMENT_ACTIVE_PROCESS_STATUSES
            ],
            _PROCESS_PRIORITY,
        )
        active = []
        for experiment in experiments:
            if experiment["status"] in EXPERIMENT_TERMINAL_STATUSES:
                continue
            experiment_sandboxes = [
                sandbox
                for sandbox in sandboxes
                if experiment["id"] in (sandbox.get("active_experiment_ids") or [])
            ]
            active.append(
                {
                    **experiment,
                    "workflow": self.policy.experiment(
                        experiment=experiment,
                        sandboxes=experiment_sandboxes,
                        evaluation=snapshot.gate_evaluations[str(experiment["id"])],
                    ),
                    "sandboxes": experiment_sandboxes,
                    "active_processes": [
                        process
                        for process in processes
                        if experiment["id"]
                        in (process.get("active_experiment_ids") or [])
                    ],
                }
            )
        return {
            "active_experiments": _sort_active(active, _EXPERIMENT_PRIORITY),
            "active_processes": processes,
        }

    def _enrich(
        self, *, project_id: str, experiments: list[Record]
    ) -> list[Record]:
        ids = tuple(
            str(experiment.get("id") or "")
            for experiment in experiments
            if experiment.get("id")
        )
        by_experiment = self.objects.by_experiment(
            project_id=project_id, experiment_ids=ids
        )
        return [
            rich_experiment_state(
                experiment,
                storage_objects=by_experiment.get(
                    str(experiment.get("id") or ""), []
                ),
            )
            for experiment in experiments
        ]


@dataclass(slots=True)
class ProjectDashboardQuery:
    """One project snapshot backs both the rich Home and compact orientation."""

    snapshots: ResearchSnapshots
    workflow: StatusAndNextQuery
    artifacts: RecordQuery
    review_queue: RecordQuery
    recent_events: RecordQuery
    health: Callable[[], dict[str, object]]
    current: RecordQuery

    def __call__(self, *, project_id: str) -> Record:
        snapshot = self.snapshots.read(
            project_id=project_id, hydrate_all_experiments=True
        )
        status, work, experiments = self.workflow.project_models(
            snapshot=snapshot,
            sandboxes=self.workflow.sandboxes.for_project(project_id=project_id),
        )
        artifacts = self.artifacts(project_id=project_id)["artifacts"]
        reviews = self.review_queue(project_id=project_id)
        events = self.recent_events(project_id=project_id, limit=25)["events"]
        claims = status["project"]["active_claims"]
        active_experiments = work["active_experiments"]
        active_processes = work["active_processes"]
        active_experiment = active_experiments[0] if active_experiments else None
        return {
            "project": status["project"],
            "claims": claims,
            "experiments": experiments,
            "active_experiments": active_experiments,
            "active_processes": active_processes,
            "artifacts": artifacts,
            "reviews": reviews,
            "pending_change_sets": [],
            "recent_events": events,
            "stats": {
                "claims": len(claims),
                "experiments": len(experiments),
                "active_experiments": len(active_experiments),
                "active_processes": len(active_processes),
                "artifacts": len(artifacts),
                "open_reviews": len(reviews["requests"]),
            },
            "workflow": (
                active_experiment.get("workflow")
                if active_experiment
                else status["workflow"]
            ),
            "active_experiment": active_experiment,
            "mlflow": self.health(),
        }

    def current_project(self, *, tenant_id: str | None = None) -> Record:
        result = self.current(tenant_id=tenant_id)
        project = result.get("project") or {}
        project_id = str(project.get("id") or "")
        if not result.get("exists") or not project_id:
            return result
        snapshot = self.snapshots.read(
            project_id=project_id,
            dashboard_facts=True,
            hydrate_selected_experiment=False,
        )
        return {**result, "at_a_glance": self._at_a_glance(snapshot)}

    def _at_a_glance(self, snapshot: ResearchSnapshot) -> Record:
        latest = snapshot.latest_published_reflection
        terminal = [
            item
            for item in snapshot.experiments
            if str(item.get("status")) in EXPERIMENT_TERMINAL_STATUSES
        ]
        active = [
            item
            for item in snapshot.experiments
            if str(item.get("status")) not in EXPERIMENT_TERMINAL_STATUSES
        ]
        covered = {
            str(item.get("id"))
            for item in ((latest or {}).get("corpus") or {}).get(
                "terminal_experiments", []
            )
            if isinstance(item, dict)
        }
        since = [item for item in terminal if str(item.get("id")) not in covered]
        changed: list[str] = []
        for event in snapshot.claim_events_since_reflection:
            claim_id = str(event.get("target_id") or "")
            if (
                claim_id
                and claim_id not in changed
                and _event_payload(event).get("source_reflection_id")
                != (latest or {}).get("id")
            ):
                changed.append(claim_id)
        reflection = None
        if latest is not None:
            graph = _artifact_link(latest, PROJECT_GRAPH_ROLES, "project_graph")
            document = _artifact_link(
                latest, ("reflection_doc", "synthesis_doc"), "reflection_doc"
            )
            reflection = {
                "reflection_id": latest.get("id"),
                "time": latest.get("published_at"),
                "reflection_doc_artifact_id": (
                    document.get("artifact_id") if document else None
                ),
                "project_graph_artifact_id": (
                    graph.get("artifact_id") if graph else None
                ),
            }
        covered_count = len(covered & {str(item.get("id")) for item in terminal})
        return {
            "summary": _glance_summary(
                latest=latest,
                terminal_count=len(terminal),
                covered_count=covered_count,
                experiments_since=len(since),
                claims_changed=len(changed),
            ),
            "recent": {
                "experiments": project_rows(
                    sorted(
                        snapshot.experiments,
                        key=lambda row: str(
                            row.get("updated_at") or row.get("created_at") or ""
                        ),
                        reverse=True,
                    )[:5],
                    ("id", "name", "status"),
                ),
                "claims": project_rows(
                    snapshot.recent_claims,
                    ("id", "status", "confidence", "statement"),
                ),
            },
            "project_reflection": reflection,
            "since_reflection": {
                "finished_experiment_ids": [str(item.get("id")) for item in since],
                "changed_claim_ids": changed,
                "active_experiment_ids": [str(item.get("id")) for item in active],
            },
            "open_reflection_id": (
                snapshot.open_reflection.get("id") if snapshot.open_reflection else None
            ),
        }


def _sort_active(items: list[Record], priority: dict[str, int]) -> list[Record]:
    recency = sorted(
        items,
        key=lambda item: item.get("updated_at") or item.get("created_at") or "",
        reverse=True,
    )
    return sorted(recency, key=lambda item: priority.get(str(item.get("status")), 99))


def _process_view(
    *, sandbox: Record, experiment: Record | None, experiments: list[Record]
) -> Record:
    result = {**sandbox, "process_type": "sandbox"}
    if experiment is not None:
        result["experiment"] = {
            key: experiment[key] for key in ("id", "intent", "status", "attempt_index")
        }
    if experiments:
        result["active_experiments"] = [
            {key: item[key] for key in ("id", "intent", "status", "attempt_index")}
            for item in experiments
        ]
    return result


def _slim_status(full: Record) -> Record:
    workflow = full.get("workflow") or {}
    project = full.get("project") or {}
    experiment = full.get("experiment")
    if experiment is None:
        result: Record = {
            "scope": "project",
            "experiment": None,
            "workflow": workflow,
            "project": {
                "id": project.get("id"),
                "name": project.get("name"),
                "summary": project.get("summary"),
                "claims": project_rows(
                    project.get("active_claims", []),
                    ("id", "status", "confidence", "statement"),
                ),
            },
        }
    else:
        result = {
            "scope": "experiment",
            "workflow": workflow,
            "experiment": {
                "id": experiment.get("id"),
                "name": experiment.get("name"),
                "status": experiment.get("status"),
                "attempt_index": experiment.get("attempt_index"),
                "intent": experiment.get("intent"),
                "conclusion": experiment.get("conclusion"),
                "updated_at": experiment.get("updated_at"),
                "tested_claim_ids": [
                    claim.get("id") for claim in experiment.get("tested_claims", [])
                ],
                "current_attempt_artifacts": project_rows(
                    experiment.get("current_attempt_artifacts", []),
                    _SLIM_ARTIFACT_FIELDS,
                ),
                "reviews": project_rows(
                    experiment.get("reviews", []), _SLIM_REVIEW_FIELDS
                ),
            },
            "sandbox": _sandbox_summary(full.get("sandboxes", [])),
            "project": {"id": project.get("id"), "name": project.get("name")},
        }
    if full.get("project_reflection"):
        result["project_reflection"] = full["project_reflection"]
    if full.get("litreview"):
        result["litreview"] = full["litreview"]
    return result


def _sandbox_summary(sandboxes: list[Record]) -> Record:
    active = next(
        (
            sandbox
            for sandbox in sandboxes
            if sandbox.get("status") in EXPERIMENT_ACTIVE_PROCESS_STATUSES
        ),
        None,
    )
    if active is not None:
        return {
            "active": True,
            **project_fields(active, _SANDBOX_SUMMARY_FIELDS),
        }
    last = sandboxes[0] if sandboxes else None
    return {
        "active": False,
        "last_status": last.get("status") if last else None,
        "note": "No active sandbox for this experiment — call sandbox.request to create or reuse one.",
    }


def _event_payload(event: Record) -> Record:
    try:
        payload = json.loads(str(event.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_link(
    reflection: Record, roles: tuple[str, ...], canonical_role: str
) -> Record | None:
    attempt = reflection.get("attempt_index")
    candidates = [
        artifact
        for artifact in reflection.get("artifacts", [])
        if artifact.get("role") in roles
        and artifact.get("attempt_index") == attempt
    ]
    if not candidates:
        return None
    rank = {role: index for index, role in enumerate(roles)}
    artifact = min(
        candidates,
        key=lambda item: (
            rank.get(str(item.get("role")), len(roles)),
            -(item.get("submitted_order") or 0),
        ),
    )
    return {
        "label": (
            "Current project graph"
            if canonical_role == "project_graph"
            else "Latest reflection doc"
        ),
        "kind": "artifact",
        "role": canonical_role,
        "legacy_role": (
            artifact.get("role")
            if artifact.get("role") != canonical_role
            else None
        ),
        "artifact_id": artifact.get("id"),
        "path": artifact.get("path"),
        "read_with": "artifact.find",
        "read_args": {"artifact_id": artifact.get("id")},
    }


def _glance_summary(
    *,
    latest: Record | None,
    terminal_count: int,
    covered_count: int,
    experiments_since: int,
    claims_changed: int,
) -> str:
    if latest is None:
        summary = f"No published reflection; 0/{terminal_count} finished experiments covered; {terminal_count} finished experiments since."
        return summary + (" New reflection recommended." if terminal_count >= 3 else "")
    pieces = [
        f"Latest reflection covers {covered_count}/{terminal_count} finished experiments"
    ]
    if experiments_since:
        pieces.append(f"{experiments_since} finished experiments since")
    if claims_changed:
        pieces.append(f"{claims_changed} claims changed since")
    if len(pieces) == 1:
        pieces.append("no newer experiment or claim changes detected")
    summary = "; ".join(pieces) + "."
    return summary + (" New reflection recommended." if experiments_since >= 3 else "")


__all__ = ["ProjectDashboardQuery", "StatusAndNextQuery"]
