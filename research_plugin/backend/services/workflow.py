"""Workflow orientation and next-action logic."""

from __future__ import annotations

from typing import Any

from .experiments import ExperimentService
from .permissions import RESOURCE_ROLES
from .resources import ResourceService
from .reviews import ReviewService
from .sandboxes import SandboxService
from ..state.store import StateStore, row_to_dict, rows_to_dicts


TERMINAL_EXPERIMENT_STATUSES = {"complete", "failed", "abandoned"}
ACTIVE_PROCESS_STATUSES = {"provisioning", "running"}
EXPERIMENT_STATUS_PRIORITY = {
    "running": 0,
    "experiment_review": 1,
    "design_review": 2,
    "ready_to_run": 3,
    "planned": 4,
}
PROCESS_STATUS_PRIORITY = {
    "running": 0,
    "provisioning": 1,
}


class WorkflowService:
    """Computes status and next actions from durable state."""

    def __init__(
        self,
        *,
        store: StateStore,
        experiments: ExperimentService,
        reviews: ReviewService,
        sandboxes: SandboxService,
        resources: ResourceService,
    ) -> None:
        self.store = store
        self.experiments = experiments
        self.reviews = reviews
        self.sandboxes = sandboxes
        self.resources = resources

    def status_and_next(
        self, *, project_id: str | None = None, experiment_id: str | None = None
    ) -> dict[str, Any]:
        project_id, experiment_id = self._resolve_scope(
            project_id=project_id,
            experiment_id=experiment_id,
        )

        with self.store.transaction() as conn:
            project = row_to_dict(
                row=conn.execute(
                    "SELECT * FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
            )
            claim_rows = conn.execute(
                "SELECT id, statement, status, confidence FROM claims WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
            exp_rows = conn.execute(
                "SELECT id, intent, status, attempt_index FROM experiments WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
            experiment = (
                self.experiments.get_state(
                    experiment_id=experiment_id,
                    project_id=project_id,
                    conn=conn,
                )
                if experiment_id
                else None
            )
            resource_refresh = (
                self._refresh_experiment_resources(conn=conn, experiment=experiment)
                if experiment
                else {"count": 0, "changed": []}
            )
            if resource_refresh["count"]:
                experiment = self.experiments.get_state(
                    experiment_id=experiment_id,
                    project_id=project_id,
                    conn=conn,
                )
            sandboxes = (
                self.sandboxes.sandboxes_for_experiment(conn=conn, experiment_id=experiment_id)
                if experiment_id
                else []
            )
            workflow = (
                self._workflow_for(conn=conn, experiment=experiment)
                if experiment
                else {
                    "current_gate": "project_setup",
                    "next_action": "create_claim_or_experiment",
                    "allowed_actions": ["claim.create", "experiment.create"],
                    "blocked_actions": [],
                    "missing_evidence": [],
                    "revision_context": "",
                }
            )
            result = {
                "project": {
                    **(project or {}),
                    "active_claims": rows_to_dicts(rows=claim_rows),
                    "active_experiments": rows_to_dicts(rows=exp_rows),
                },
                "experiment": experiment,
                "sandboxes": sandboxes,
                "workflow": workflow,
            }
            if resource_refresh["count"]:
                result["resource_refresh"] = resource_refresh
            return result

    def active_work(self, *, project_id: str | None = None) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(
                conn=conn,
                project_id=project_id,
            )
            rows = conn.execute(
                "SELECT id FROM experiments WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
            experiments: list[dict[str, Any]] = []
            for row in rows:
                experiment = self.experiments.get_state(
                    experiment_id=row["id"],
                    project_id=project_id,
                    conn=conn,
                )
                resource_refresh = self._refresh_experiment_resources(
                    conn=conn,
                    experiment=experiment,
                )
                if resource_refresh["count"]:
                    experiment = self.experiments.get_state(
                        experiment_id=row["id"],
                        project_id=project_id,
                        conn=conn,
                    )
                    experiment["resource_refresh"] = resource_refresh
                experiments.append(experiment)

            experiments_by_id = {
                experiment["id"]: experiment for experiment in experiments
            }
            sandboxes = self.sandboxes.sandboxes_for_project(conn=conn, project_id=project_id)
            active_processes = self._sort_active_processes(
                processes=[
                    self._process_view(
                        sandbox=sandbox,
                        experiment=experiments_by_id.get(str(sandbox.get("experiment_id"))),
                    )
                    for sandbox in sandboxes
                    if sandbox.get("status") in ACTIVE_PROCESS_STATUSES
                ]
            )

            active_experiments: list[dict[str, Any]] = []
            for experiment in experiments:
                if experiment["status"] in TERMINAL_EXPERIMENT_STATUSES:
                    continue
                experiment_sandboxes = [
                    sandbox for sandbox in sandboxes if sandbox.get("experiment_id") == experiment["id"]
                ]
                experiment_active_processes = [
                    process
                    for process in active_processes
                    if process.get("experiment_id") == experiment["id"]
                ]
                active_experiments.append(
                    {
                        **experiment,
                        "workflow": self._workflow_for(
                            conn=conn,
                            experiment=experiment,
                        ),
                        "sandboxes": experiment_sandboxes,
                        "active_processes": experiment_active_processes,
                    }
                )

            return {
                "active_experiments": self._sort_active_experiments(
                    experiments=active_experiments,
                ),
                "active_processes": active_processes,
            }

    def _resolve_scope(
        self, *, project_id: str | None, experiment_id: str | None
    ) -> tuple[str, str | None]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(
                conn=conn,
                project_id=project_id,
            )
            if experiment_id is None:
                row = conn.execute(
                    "SELECT id FROM experiments WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                experiment_id = row["id"] if row else None
            return project_id, experiment_id
        finally:
            conn.close()

    def _workflow_for(self, *, conn, experiment: dict[str, Any]) -> dict[str, Any]:
        status = experiment["status"]
        exp_id = experiment["id"]
        roles = {
            res.get("association_role")
            for res in experiment.get("current_attempt_resources", [])
            if not res.get("missing")
        }
        if status == "planned":
            if "plan" not in roles:
                return self._next(
                    gate="plan_required",
                    action="write_or_sync_plan_resource",
                    allowed=["resource.register_file", "resource.associate"],
                    missing=["experiment plan resource"],
                    revision=experiment.get("revision_context", ""),
                )
            return self._next(
                gate="design_review_required",
                action="submit_design_for_review",
                allowed=["experiment.transition"],
                revision=experiment.get("revision_context", ""),
            )
        if status == "design_review":
            review_next = self._review_next(
                conn=conn,
                experiment=experiment,
                role="design_reviewer",
                skill="design-review",
            )
            if review_next:
                return review_next
        if status == "ready_to_run":
            return self._next(
                gate="execution_ready",
                action="start_running",
                allowed=["sandbox.request", "experiment.transition"],
            )
        if status == "running":
            sandboxes = self.sandboxes.sandboxes_for_experiment(conn=conn, experiment_id=exp_id)
            active = any(sb.get("status") in ACTIVE_PROCESS_STATUSES for sb in sandboxes)
            if "result" not in roles:
                return self._next(
                    gate="execution_active" if active else "execution_ready",
                    action="run_experiment_and_sync_results",
                    allowed=[
                        "sandbox.request",
                        "sandbox.terminal",
                        "sandbox.get",
                        "resource.register_file",
                        "resource.associate",
                        "resource.sync_changed_files",
                    ],
                    missing=["result resource"],
                    resource_guidance=self._result_resource_guidance(),
                )
            return self._next(
                gate="experiment_review_required",
                action="submit_results_for_review",
                allowed=["experiment.transition"],
            )
        if status == "experiment_review":
            review_next = self._review_next(
                conn=conn,
                experiment=experiment,
                role="experiment_reviewer",
                skill="experiment-review",
            )
            if review_next:
                return review_next
        if status == "complete":
            return self._next(
                gate="terminal",
                action="none",
                allowed=[],
                blocked=[
                    {"action": "mutate_experiment", "reason": "experiment complete"}
                ],
            )
        if status in {"failed", "abandoned"}:
            return self._next(gate="terminal", action="none", allowed=[])
        return self._next(
            gate="unknown",
            action="inspect_experiment",
            allowed=["experiment.get_state"],
        )

    def _refresh_experiment_resources(
        self, *, conn, experiment: dict[str, Any]
    ) -> dict[str, Any]:
        if experiment["status"] in {"complete", "failed", "abandoned"}:
            return {"count": 0, "changed": []}
        return self.resources.refresh_target_resources(
            conn=conn,
            target_type="experiment",
            target_id=experiment["id"],
            attempt_index=experiment["attempt_index"],
        )

    def _review_next(
        self, *, conn, experiment: dict[str, Any], role: str, skill: str
    ) -> dict[str, Any] | None:
        exp_id = experiment["id"]
        gate = experiment["status"]
        action_name = {
            "design_reviewer": "design_review",
            "experiment_reviewer": "experiment_review",
        }[role]
        verdict = self.reviews.latest_verdict(
            conn=conn,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )
        if verdict == "pass":
            next_action = (
                "mark_ready_to_run"
                if role == "design_reviewer"
                else "complete_experiment"
            )
            return self._next(
                gate=f"{action_name}_passed",
                action=next_action,
                allowed=["experiment.transition"],
            )

        request = self.reviews.open_request(
            conn=conn,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )
        if request is None:
            return self._next(
                gate=gate,
                action=f"launch_{action_name}er",
                allowed=["review.request"],
                review_gate=self._review_gate(
                    role=role,
                    skill=skill,
                    status="none",
                    target_id=exp_id,
                ),
            )
        if request["status"] == "requested":
            return self._next(
                gate=gate,
                action=f"launch_{action_name}er",
                allowed=["review.status"],
                review_gate=self._review_gate(
                    role=role,
                    skill=skill,
                    status="requested",
                    target_id=exp_id,
                    request=request,
                ),
            )
        return self._next(
            gate=gate,
            action=f"wait_for_{action_name}",
            allowed=["review.status"],
            review_gate=self._review_gate(
                role=role,
                skill=skill,
                status=str(request["status"]),
                target_id=exp_id,
                request=request,
            ),
        )

    def _review_gate(
        self,
        *,
        role: str,
        skill: str,
        status: str,
        target_id: str,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        labels = {
            "none": "Needs reviewer",
            "requested": "Reviewer pending",
            "started": "Reviewer active",
        }
        gate = {
            "role": role,
            "skill": skill,
            "target_type": "experiment",
            "target_id": target_id,
            "status": status,
            "label": labels.get(status, status),
            "read_only": True,
        }
        if request:
            gate["request_id"] = request["id"]
            gate["expires_at"] = request["expires_at"]
        return gate

    def _result_resource_guidance(self) -> dict[str, Any]:
        return {
            "target_type": "experiment",
            "association_role": "result",
            "allowed_resource_roles": sorted(RESOURCE_ROLES),
        }

    def _next(
        self,
        *,
        gate: str,
        action: str,
        allowed: list[str],
        blocked: list[dict[str, str]] | None = None,
        missing: list[str] | None = None,
        revision: str = "",
        review_gate: dict[str, Any] | None = None,
        resource_guidance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "current_gate": gate,
            "next_action": action,
            "allowed_actions": allowed,
            "blocked_actions": blocked or [],
            "missing_evidence": missing or [],
            "revision_context": revision,
        }
        if review_gate is not None:
            result["review_gate"] = review_gate
        if resource_guidance is not None:
            result["resource_guidance"] = resource_guidance
        return result

    def _sort_active_experiments(
        self, *, experiments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        experiments = sorted(
            experiments,
            key=lambda item: item.get("updated_at") or item.get("created_at") or "",
            reverse=True,
        )
        return sorted(
            experiments,
            key=lambda item: EXPERIMENT_STATUS_PRIORITY.get(
                str(item.get("status")), 99
            ),
        )

    def _sort_active_processes(
        self, *, processes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        processes = sorted(
            processes,
            key=lambda item: item.get("updated_at") or item.get("created_at") or "",
            reverse=True,
        )
        return sorted(
            processes,
            key=lambda item: PROCESS_STATUS_PRIORITY.get(str(item.get("status")), 99),
        )

    def _process_view(
        self, *, sandbox: dict[str, Any], experiment: dict[str, Any] | None
    ) -> dict[str, Any]:
        process = {
            **sandbox,
            "process_type": "sandbox",
        }
        if experiment is not None:
            process["experiment"] = {
                "id": experiment["id"],
                "intent": experiment["intent"],
                "status": experiment["status"],
                "attempt_index": experiment["attempt_index"],
            }
        return process
