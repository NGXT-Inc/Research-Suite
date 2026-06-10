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

# Agent-facing projection of status_and_next. The next-action decision is
# computed (in _workflow_for) from just status + resource roles + the review
# verdict, so the rest of the embedded get_state — the duplicate all-attempts
# `resources` list, per-resource version bookkeeping (version_token, mtime_ns,
# *_version_id, git_commit, timestamps), full review prose, and every *other*
# experiment's intent — is pure context bloat for a call the agent polls
# constantly. The UI keeps the full shape (it calls the service method
# directly); only the MCP tool is slimmed. See docs/MCP_SERVER_CONTRACT.md.
_SLIM_RESOURCE_FIELDS = ("id", "association_role", "path", "kind", "missing", "size_bytes")
_SANDBOX_SUMMARY_FIELDS = (
    "sandbox_id", "status", "gpu", "cpu", "memory",
    "ssh_host", "ssh_port", "ssh_user", "workdir", "sandbox_data_dir", "expires_at",
)


def slim_status_and_next(full: dict[str, Any]) -> dict[str, Any]:
    """Project the rich status_and_next result down to what the agent needs."""
    workflow = full.get("workflow") or {}
    project = full.get("project") or {}
    experiment = full.get("experiment")

    if experiment is None:
        # Project-scoped orientation, reached only at project setup (once any
        # experiment exists, status_and_next auto-resolves to the latest one).
        # Surface existing claims compactly so the agent doesn't re-create them;
        # there are no experiments to list here by definition.
        return {
            "scope": "project",
            "experiment": None,
            "workflow": workflow,
            "project": {
                "id": project.get("id"),
                "name": project.get("name"),
                "summary": project.get("summary"),
                "claims": [
                    {
                        "id": claim.get("id"),
                        "status": claim.get("status"),
                        "confidence": claim.get("confidence"),
                        "statement": claim.get("statement"),
                    }
                    for claim in project.get("active_claims", [])
                ],
            },
        }

    result: dict[str, Any] = {
        "scope": "experiment",
        "workflow": workflow,
        "experiment": _slim_experiment(experiment),
        "sandbox": _sandbox_summary(full.get("sandboxes", [])),
        "project": {"id": project.get("id"), "name": project.get("name")},
    }
    if full.get("resource_refresh"):
        result["resource_refresh"] = full["resource_refresh"]
    return result


def _slim_experiment(exp: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": exp.get("id"),
        "status": exp.get("status"),
        "attempt_index": exp.get("attempt_index"),
        "intent": exp.get("intent"),
        "conclusion": exp.get("conclusion"),
        "updated_at": exp.get("updated_at"),
        "tested_claim_ids": [claim.get("id") for claim in exp.get("tested_claims", [])],
        "current_attempt_resources": [
            {field: res.get(field) for field in _SLIM_RESOURCE_FIELDS}
            for res in exp.get("current_attempt_resources", [])
        ],
        "reviews": [
            {
                "id": review.get("id"),
                "role": review.get("role"),
                "verdict": review.get("verdict"),
                "created_at": review.get("created_at"),
            }
            for review in exp.get("reviews", [])
        ],
    }


def _sandbox_summary(sandboxes: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse the sandbox row(s) to 'is there an active one, and if so what'."""
    active = next(
        (sb for sb in sandboxes if sb.get("status") in ACTIVE_PROCESS_STATUSES),
        None,
    )
    if active is not None:
        summary: dict[str, Any] = {"active": True}
        summary.update({field: active.get(field) for field in _SANDBOX_SUMMARY_FIELDS})
        return summary
    last = sandboxes[0] if sandboxes else None
    return {
        "active": False,
        "last_status": last.get("status") if last else None,
        "note": "No active sandbox for this experiment — call sandbox.request to create or reuse one.",
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

    def status_and_next_agent(
        self, *, project_id: str | None = None, experiment_id: str | None = None
    ) -> dict[str, Any]:
        """Agent/MCP-facing status_and_next: the full computation, slim output.

        Backs the `workflow.status_and_next` tool. The UI calls
        `status_and_next` directly for the rich shape; agents get this
        projection so a constantly-polled orientation call stops flooding the
        context window.
        """
        return slim_status_and_next(
            self.status_and_next(project_id=project_id, experiment_id=experiment_id)
        )

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
                        "sandbox.sync",
                        "resource.register_file",
                        "resource.associate",
                    ],
                    missing=["result resource"],
                    resource_guidance=self._result_resource_guidance(),
                )
            return self._next(
                gate="experiment_review_required",
                action=(
                    "submit_results_for_review (call only once the experiment "
                    "is fully complete and every success criterion in the "
                    "experiment intent is satisfied; do NOT call if the "
                    "experiment should continue running; continue with "
                    "sandbox.* and resource.* calls instead and only "
                    "transition once the work is truly done)"
                ),
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
            "dataset_guidance": (
                "Prefer CPU-only sandboxes for data inspection and data engineering "
                "unless the command needs GPU. Work in $RP_SYNC_DIR for scripts, configs, "
                "metrics, and compact results that should rsync back to the local experiment "
                "folder. Download large datasets, caches, checkpoints, parquet files, and "
                "other heavy intermediates to $RP_UNSYNCED_DIR / $RP_DATASET_DIR. If a large "
                "artifact deliberately must persist locally, place it under "
                "$RP_SYNC_DIR/artifacts_to_keep. Prefer saving an experiment-folder data.md "
                "that records dataset sources, splits, filters, schema/row-count notes, "
                "caveats, and where ephemeral data lives."
            ),
            "sync_guidance": (
                "After the sandbox is running, make experiment file changes inside "
                "$RP_SYNC_DIR. Before registering or associating result resources, call "
                "sandbox.sync so remote synced files exist under the experiment's local "
                "sync directory. The backend also rsyncs periodically while the sandbox is "
                "running, but explicit sandbox.sync is the durable handoff before workflow "
                "mutations."
            ),
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
