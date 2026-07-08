"""Workflow orientation and next-action logic."""

from __future__ import annotations

from typing import Any

from ..artifacts.roles import (
    PROJECT_GRAPH_ROLE,
    PROJECT_GRAPH_ROLES,
    REFLECTION_LENS_DOC_ROLE,
    RESOURCE_ROLES,
    external_reflection_target_type,
)
from ..domain.gates import ReviewRequirement, RoleRequirement
from ..domain.paths import experiment_folder_rel
from ..domain.reflection_policy import (
    external_reflection_signal,
    idle_reflection_hint,
    reflection_create_block_reason,
)
from ..domain.reflection_gates import REFLECTION_GATE_TABLE
from ..domain.workflow_gates import (
    ACTIVE_PROCESS_STATUSES,
    GATE_TABLE,
    TERMINAL_STATUSES,
)
from ..ports.workflow_readers import (
    ExperimentWorkflowReader,
    ReflectionWorkflowReader,
    ReviewWorkflowReader,
    SandboxWorkflowReader,
)
from .workflow_views import slim_status_and_next, slim_synthesis
from ..state.store import BaseStateStore, row_to_dict, rows_to_dicts


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
        store: BaseStateStore,
        experiments: ExperimentWorkflowReader,
        reviews: ReviewWorkflowReader,
        sandboxes: SandboxWorkflowReader,
        reflections: ReflectionWorkflowReader,
        storage_enabled: bool = False,
        storage_guidance: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.experiments = experiments
        self.reviews = reviews
        self.sandboxes = sandboxes
        self.reflections = reflections
        self.storage_enabled = bool(storage_enabled)
        # Composition-injected storage guidance block (object_storage prose).
        # The workflow embeds the dict it is handed instead of importing the
        # storage module; compositions pass storage_guidance(enabled=...).
        self.storage_guidance = dict(storage_guidance or {"enabled": self.storage_enabled})

    def status_and_next(
        self, *, project_id: str | None = None, experiment_id: str | None = None
    ) -> dict[str, Any]:
        # Whether the caller scoped to an experiment explicitly: only the
        # auto-resolved (project-level) orientation call is eligible for the
        # idle reflection takeover below.
        requested_experiment_id = experiment_id
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
                "SELECT id, statement, scope, status, confidence, created_at FROM claims WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            exp_rows = conn.execute(
                "SELECT id, intent, status, attempt_index FROM experiments WHERE project_id = ? ORDER BY created_at, id",
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
            sandboxes = (
                self.sandboxes.sandboxes_for_experiment(conn=conn, experiment_id=experiment_id)
                if experiment_id
                else []
            )
            workflow = (
                self._workflow_for(conn=conn, experiment=experiment)
                if experiment
                else self._next(
                    gate="project_setup",
                    action="create_claim_or_experiment",
                    allowed=["claim.create", "experiment.create"],
                )
            )
            idle = all(
                str(row["status"]) in TERMINAL_STATUSES for row in exp_rows
            )
            reflection = self._project_reflection(
                conn=conn, project_id=project_id, idle=idle
            )
            if requested_experiment_id is None and idle:
                takeover = self._reflection_workflow_takeover(reflection=reflection)
                if takeover is not None:
                    workflow = takeover
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
            if reflection is not None:
                result["project_reflection"] = reflection
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
                "SELECT id FROM experiments WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            experiments: list[dict[str, Any]] = []
            for row in rows:
                experiment = self.experiments.get_state(
                    experiment_id=row["id"],
                    project_id=project_id,
                    conn=conn,
                )
                experiments.append(experiment)

            experiments_by_id = {
                experiment["id"]: experiment for experiment in experiments
            }
            sandboxes = self.sandboxes.sandboxes_for_project(conn=conn, project_id=project_id)
            active_processes = self._sort_active_processes(
                processes=[
                    self._process_view(
                        sandbox=sandbox,
                        experiment=experiments_by_id.get(
                            str((sandbox.get("active_experiment_ids") or [""])[0])
                        ),
                        experiments=[
                            experiments_by_id[exp_id]
                            for exp_id in sandbox.get("active_experiment_ids") or []
                            if exp_id in experiments_by_id
                        ],
                    )
                    for sandbox in sandboxes
                    if sandbox.get("status") in ACTIVE_PROCESS_STATUSES
                ]
            )

            active_experiments: list[dict[str, Any]] = []
            for experiment in experiments:
                if experiment["status"] in TERMINAL_STATUSES:
                    continue
                experiment_sandboxes = [
                    sandbox
                    for sandbox in sandboxes
                    if experiment["id"] in (sandbox.get("active_experiment_ids") or [])
                ]
                experiment_active_processes = [
                    process
                    for process in active_processes
                    if experiment["id"] in (process.get("active_experiment_ids") or [])
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
                    "SELECT id FROM experiments WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                experiment_id = row["id"] if row else None
            return project_id, experiment_id
        finally:
            conn.close()

    def _workflow_for(self, *, conn, experiment: dict[str, Any]) -> dict[str, Any]:
        """Guidance derived from the same GATE_TABLE that enforces transitions.

        Walk the status's forward transition: a review requirement delegates to
        the review flow; the first resource role missing from the current
        attempt yields that requirement's gate payload; all requirements met
        yields the transition's ready payload.
        """
        status = experiment["status"]
        forward = GATE_TABLE.get(status)
        if forward is None:
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
        if forward.review is not None:
            return self._review_next(
                conn=conn,
                target_type="experiment",
                target=experiment,
                review=forward.review,
            )
        roles = {
            res.get("association_role")
            for res in experiment.get("current_attempt_resources", [])
            if not res.get("missing")
        }
        for requirement in forward.requirements:
            if requirement.role in roles:
                continue
            return self._next(
                gate=self._requirement_gate(
                    conn=conn, experiment=experiment, requirement=requirement
                ),
                action=requirement.action,
                allowed=list(requirement.allowed),
                missing=[requirement.missing],
                resource_guidance=self._resource_guidance(
                    key=requirement.guidance_key, experiment=experiment
                ),
                revision=experiment.get("revision_context", ""),
            )
        # Every required artifact exists — now run the same deep lints the
        # transition runs, so "ready" is never announced for an artifact the
        # transition would reject (the agent fixes the file instead of
        # discovering the problem via a failed transition).
        for requirement in forward.requirements:
            if not requirement.validator:
                continue
            problems = self.experiments.validator_problems(
                conn=conn, experiment_id=experiment["id"], name=requirement.validator
            )
            if problems:
                return self._next(
                    gate=f"{requirement.role}_invalid",
                    action=f"fix_{requirement.role}_resource",
                    allowed=list(requirement.allowed),
                    missing=problems,
                    resource_guidance=self._resource_guidance(
                        key=requirement.guidance_key, experiment=experiment
                    ),
                    revision=experiment.get("revision_context", ""),
                )
        return self._next(
            gate=forward.ready_gate,
            action=forward.ready_action,
            allowed=list(forward.ready_allowed),
            revision=experiment.get("revision_context", "") if status != "ready_to_run" else "",
        )

    def _requirement_gate(
        self, *, conn, experiment: dict[str, Any], requirement: RoleRequirement
    ) -> str:
        """A requirement's gate name, with the one dynamic case: the running
        status's execution gate reflects whether a sandbox is actually live."""
        if experiment["status"] == "running" and requirement.gate == "execution_ready":
            sandboxes = self.sandboxes.sandboxes_for_experiment(
                conn=conn, experiment_id=experiment["id"]
            )
            if any(sb.get("status") in ACTIVE_PROCESS_STATUSES for sb in sandboxes):
                return "execution_active"
        return requirement.gate

    def _resource_guidance(
        self, *, key: str, experiment: dict[str, Any]
    ) -> dict[str, Any] | None:
        # Guidance names the experiment's actual folder (experiments/<name>/)
        # so the agent is told exactly where the artifact lives.
        folder = experiment_folder_rel(
            experiment_id=str(experiment.get("id") or ""),
            name=str(experiment.get("name") or ""),
        )
        if key == "plan":
            return self._plan_resource_guidance(folder=folder)
        if key == "result":
            return self._result_resource_guidance()
        if key == "report":
            return self._report_resource_guidance(folder=folder)
        if key == "graph":
            return self._graph_resource_guidance(folder=folder)
        return None

    def _review_next(
        self,
        *,
        conn,
        target_type: str,
        target: dict[str, Any],
        review: ReviewRequirement,
    ) -> dict[str, Any]:
        target_id = target["id"]
        gate = target["status"]
        if target_type == "synthesis" and gate == "synthesis_review":
            gate = "reflection_review"
        role, skill, action_name = review.role, review.skill, review.action_name
        transition_tool = (
            "reflection.transition"
            if target_type == "synthesis"
            else "experiment.transition"
        )
        gate_state = self.reviews.gate_state(
            conn=conn,
            target_type=target_type,
            target_id=target_id,
            role=role,
        )
        if gate_state["satisfied"]:
            return self._next(
                gate=f"{action_name}_passed",
                action=review.pass_action,
                allowed=[transition_tool],
            )

        request = self.reviews.open_request(
            conn=conn,
            target_type=target_type,
            target_id=target_id,
            role=role,
        )
        if request is None:
            # An attested-only pass under require_verified_reviews lands here
            # (its request is already submitted): say why the gate still blocks.
            blocked_reason = gate_state.get("blocked_reason")
            return self._next(
                gate=gate,
                action=f"launch_{action_name}er",
                allowed=["review.request"],
                missing=[blocked_reason] if blocked_reason else [],
                review_gate=self._review_gate(
                    role=role,
                    skill=skill,
                    status="attested_blocked" if blocked_reason else "none",
                    target_type=target_type,
                    target_id=target_id,
                ),
            )
        if request["status"] == "requested":
            return self._next(
                gate=gate,
                action=f"launch_{action_name}er",
                allowed=["review.status", "review.request"],
                review_gate=self._review_gate(
                    role=role,
                    skill=skill,
                    status="requested",
                    target_type=target_type,
                    target_id=target_id,
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
                target_type=target_type,
                target_id=target_id,
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
        target_type: str = "experiment",
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        labels = {
            "none": "Needs reviewer",
            "requested": "Reviewer pending",
            "started": "Reviewer active",
            "attested_blocked": "Verified review required",
        }
        gate = {
            "role": role,
            "skill": skill,
            "target_type": external_reflection_target_type(target_type),
            "target_id": target_id,
            "status": status,
            "label": labels.get(status, status),
            "read_only": True,
        }
        if request:
            gate["request_id"] = request["id"]
            gate["expires_at"] = request["expires_at"]
        return gate

    def _project_reflection(
        self, *, conn, project_id: str, idle: bool
    ) -> dict[str, Any] | None:
        """Project-level reflection orientation, surfaced only when relevant.

        While a reflection wave is open: the wave's slim state plus the same
        gate-table-derived guidance experiments get. Otherwise the drift
        signal decides, with an advisory nudge before a hard create block:

        - stale (>= 3 newly-terminal experiments since the last published
          reflection, or a claim flipped to contradicted): the soft
          "Consider…" hint, whatever else is in flight.
        - recommended (the project is idle AND at least one experiment has
          finished since the last published reflection): nothing is running
          and there is something new to reflect on, so reflection is worth
          suggesting as the next action (_reflection_workflow_takeover).
        - blocked (>= 5 newly-terminal experiments since the last published
          reflection): experiment.create is rejected until a project reflection
          is published; a published change spec can still materialize the next
          experiment wave.

        Nothing at all when there is nothing to say, so the constantly-polled
        orientation call stays lean until the hard threshold is reached.
        Before that threshold, whether new developments change the project's
        logic state stays the agent's call.
        """
        open_wave = self.reflections.open_reflection(conn=conn, project_id=project_id)
        if open_wave is not None:
            signal = self.reflections.reflection_signal(project_id=project_id, conn=conn)
            workflow = self._reflection_workflow_for(conn=conn, synthesis=open_wave)
            if signal.get("experiment_create_blocked"):
                workflow = self._with_experiment_create_block(
                    workflow=workflow, signal=signal
                )
            return {
                "reflection": slim_synthesis(open_wave),
                "workflow": workflow,
                "signal": external_reflection_signal(signal),
            }
        signal = self.reflections.reflection_signal(project_id=project_id, conn=conn)
        has_new_material = (
            signal["new_terminal_since_publish"] >= 1 or signal["contradicted_flip"]
        )
        recommended = idle and has_new_material
        if not signal["stale"] and not recommended:
            return None
        block: dict[str, Any] = {
            "reflection": None,
            "hint": signal["hint"] or idle_reflection_hint(signal=signal),
            "signal": external_reflection_signal(signal),
            "experiment_create_blocked": bool(signal.get("experiment_create_blocked")),
        }
        if recommended:
            block["recommended"] = True
        return block

    def _reflection_workflow_takeover(
        self, *, reflection: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The workflow block for an idle, project-level orientation call.

        An idle project's auto-resolved experiment is terminal, so its gate
        answers "none" — exactly the moment the agent is deciding what to do
        next. An open reflection wave's gate guidance takes the slot;
        otherwise the 'recommended' tier suggests starting one. The hard
        create-block tier removes experiment.create until a reflection is
        published. Explicit experiment-scoped calls never reach this (the
        project_reflection side block still carries the signal there).
        """
        if reflection is None:
            return None
        if reflection.get("reflection") is not None:
            return reflection["workflow"]
        signal = reflection.get("signal") or {}
        if signal.get("experiment_create_blocked"):
            reason = reflection.get("hint") or reflection_create_block_reason(
                signal=signal
            )
            return self._next(
                gate="reflection_required",
                action=(
                    "start_project_reflection_before_next_experiment "
                    "(run reflection.create via the project-reflection skill; "
                    "experiment.create is blocked until a project reflection is "
                    "published)"
                ),
                allowed=["reflection.create", "claim.create"],
                blocked=[{"action": "experiment.create", "reason": reason}],
                missing=[reason] if reason else [],
            )
        if not reflection.get("recommended"):
            return None
        return self._next(
            gate="reflection_suggested",
            action=(
                "consider_project_reflection (start a reflection wave with "
                "reflection.create via the project-reflection skill — or "
                "proceed with claim.create / experiment.create if you judge "
                "the project's logic state current)"
            ),
            allowed=["reflection.create", "claim.create", "experiment.create"],
            missing=[reflection["hint"]] if reflection.get("hint") else [],
        )

    def _with_experiment_create_block(
        self, *, workflow: dict[str, Any], signal: dict[str, Any]
    ) -> dict[str, Any]:
        reason = reflection_create_block_reason(signal=signal)
        blocked = [
            item
            for item in workflow.get("blocked_actions", [])
            if item.get("action") != "experiment.create"
        ]
        blocked.append({"action": "experiment.create", "reason": reason})
        return {
            **workflow,
            "allowed_actions": [
                action
                for action in workflow.get("allowed_actions", [])
                if action != "experiment.create"
            ],
            "blocked_actions": blocked,
        }

    def _reflection_workflow_for(self, *, conn, synthesis: dict[str, Any]) -> dict[str, Any]:
        """Guidance for a reflection wave, derived from REFLECTION_GATE_TABLE —
        the same walk as _workflow_for, with the roster-coverage requirement
        reported per missing lens."""
        status = synthesis["status"]
        forward = REFLECTION_GATE_TABLE.get(status)
        if forward is None:
            return self._next(gate="terminal", action="none", allowed=[])
        if forward.review is not None:
            return self._review_next(
                conn=conn,
                target_type="synthesis",
                target=synthesis,
                review=forward.review,
            )
        if status == "reflecting":
            coverage = synthesis.get("reflection_coverage", {})
            requirement = forward.requirements[0]
            if not coverage.get("complete"):
                return self._next(
                    gate=requirement.gate,
                    action=requirement.action,
                    allowed=list(requirement.allowed),
                    missing=[
                        "reflection doc for lens "
                        f"'{lens_id}' (role 'reflection_lens_doc', file <lens_id>.md)"
                        for lens_id in coverage.get("missing", [])
                    ],
                    resource_guidance=self._reflection_resource_guidance(),
                    revision=synthesis.get("revision_context", ""),
                )
            return self._next(
                gate=forward.ready_gate,
                action=forward.ready_action,
                allowed=list(forward.ready_allowed),
                revision=synthesis.get("revision_context", ""),
            )
        roles = {
            res.get("association_role")
            for res in synthesis.get("current_attempt_resources", [])
            if not res.get("missing")
        }
        for requirement in forward.requirements:
            if requirement.role in roles or (
                requirement.role == "reflection_doc" and "synthesis_doc" in roles
            ) or (
                requirement.role == PROJECT_GRAPH_ROLE
                and bool(set(PROJECT_GRAPH_ROLES) & roles)
            ):
                continue
            return self._next(
                gate=requirement.gate,
                action=requirement.action,
                allowed=list(requirement.allowed),
                missing=[requirement.missing],
                resource_guidance=self._synthesis_resource_guidance(
                    key=requirement.guidance_key
                ),
                revision=synthesis.get("revision_context", ""),
            )
        return self._next(
            gate=forward.ready_gate,
            action=forward.ready_action,
            allowed=list(forward.ready_allowed),
            revision=synthesis.get("revision_context", ""),
        )

    def _reflection_resource_guidance(self) -> dict[str, Any]:
        return {
            "target_type": "reflection",
            "association_role": REFLECTION_LENS_DOC_ROLE,
            "guidance": (
                "Fan out one read-only subagent per missing lens. Each subagent "
                "reads the project through its lens only (tell it which other "
                "lenses are running so it stays in its lane), writes its "
                "reflection to a file named <lens_id>.md (e.g. "
                "syntheses/<syn_id>/reflections/<lens_id>.md), registers it, and "
                "associates it with role 'reflection_lens_doc' for this "
                "reflection wave. See the project-reflection skill for the lens briefs."
            ),
        }

    def _synthesis_resource_guidance(self, *, key: str) -> dict[str, Any] | None:
        if key == "project_graph":
            return {
                "target_type": "reflection",
                "association_role": PROJECT_GRAPH_ROLE,
                "template": "skills/research-workflow/graph-template.md",
                "guidance": (
                    "Update the living project logic graph (one JSON file, e.g. "
                    "project/logic_graph.json): the current logic state of the "
                    "whole project — what is established, what was ruled out and "
                    "why, the open questions — as a DAG of at most 16 nodes. "
                    "Treat the lens reflections as unverified inputs: reconcile "
                    "them against the actual records, don't average them. Edit "
                    "the living graph in place and prune within the budget; "
                    "node refs may point at exp_/claim_/rev_/syn_ ids or files. "
                    "Then register the file and associate it with role "
                    "'project_graph' for this reflection wave."
                ),
            }
        if key in {"reflection_doc", "synthesis_doc"}:
            return {
                "target_type": "reflection",
                "association_role": "reflection_doc",
                "template": "skills/project-reflection/reflection-artifacts-template.md",
                "guidance": (
                    "Write the short reflection document as markdown: a critical "
                    "scientific reading of the five lens reflections with Summary, "
                    "Critical reading, and Decision / future directions "
                    "sections. Keep it under 16 KB. You may reference a few "
                    "figures with relative markdown image links (e.g. "
                    "![project graph](figures/project_graph.png)); every linked "
                    "image must exist before association. Then register the file "
                    "and associate it with role 'reflection_doc' for this reflection wave."
                ),
            }
        if key == "change_spec":
            return {
                "target_type": "reflection",
                "association_role": "change_spec",
                "template": "skills/project-reflection/reflection-artifacts-template.md",
                "guidance": (
                    "Write the change spec as JSON: claim_changes plus a "
                    "create_experiments decision with 1-3 planned experiment "
                    "specs — names, intents, tested claim refs, and (for a "
                    "multi-experiment wave) a parallelism note each. Publish "
                    "will apply this only after the reflection reviewer passes "
                    "it. Then register the file and associate it with role "
                    "'change_spec' for this reflection wave."
                ),
            }
        return None

    def _plan_resource_guidance(self, *, folder: str) -> dict[str, Any]:
        return {
            "target_type": "experiment",
            "association_role": "plan",
            "template": "skills/research-workflow/plan-template.md",
            "guidance": (
                f"Write the experiment plan as one markdown file at {folder}plan.md "
                "— the folder experiment.create made for this experiment. Keep "
                "the plan, scripts, configs, and durable inputs there; live "
                "sandboxes have their own work folders and can later be "
                "associated with this experiment. "
                "Start from the template's required sections, then register the "
                "file and associate it with role 'plan'. Consider seeding the "
                f"logic graph now too ({folder}graph.json, see "
                "skills/research-workflow/graph-template.md): an objective node "
                "costs a minute, and the story of the experiment's hard "
                "decisions should grow as you make them — not be reconstructed "
                "at the end."
            ),
        }

    def _result_resource_guidance(self) -> dict[str, Any]:
        heavy_retention = (
            "copy light files out over SSH into the local experiment folder "
            "or upload heavy files with storage.put_object, then register "
            "those retained files."
            if self.storage_enabled
            else (
                "copy retained files out over SSH into the local experiment folder, "
                "then register those retained files. Heavy-file storage is not "
                "enabled on this backend, so large sandbox-only datasets/models "
                "will not survive release."
            )
        )
        return {
            "target_type": "experiment",
            "association_role": "result",
            "allowed_resource_roles": sorted(RESOURCE_ROLES),
            "dataset_guidance": (
                "Prefer CPU-only sandboxes for data inspection and data engineering "
                "unless the command needs GPU. Work inside the sandbox work folder "
                "for scripts, configs, metrics, and compact results. Download "
                "large datasets, caches, checkpoints, parquet files, and other "
                "heavy intermediates into the sandbox's dataset/cache locations; "
                "copy out or upload only the files that "
                "must persist before releasing the sandbox. Prefer "
                "saving a data.md in the experiment folder that records dataset "
                "sources, splits, filters, schema/row-count notes, caveats, and "
                "where ephemeral data lives on the VM."
            ),
            "retention_guidance": (
                "While a sandbox is live, treat its work folder as ephemeral "
                "scratch. Before registering or associating result resources, "
                + heavy_retention
            ),
            "storage_guidance": dict(self.storage_guidance),
            "report_guidance": (
                "A results report (role 'report') is also required before "
                "submit_results — write it in the same pass as your result files. "
                "While the run produces metrics, save the figures (matplotlib PNGs) "
                "you will reference. See skills/research-workflow/report-template.md."
            ),
            "graph_guidance": (
                "A logic graph (role 'graph') is also required before "
                "submit_results — a qualitative story you author about the "
                "experiment's logical path: the hard decisions and the "
                "reasoning behind them, not an event or pipeline diagram "
                "(graph.json, a DAG of at most 16 nodes, written by hand — "
                "never script-generated). Record decisions in the graph as you "
                "make them, while the reasoning is fresh; the user watches it "
                "live. See skills/research-workflow/graph-template.md."
            ),
        }

    def _report_resource_guidance(self, *, folder: str) -> dict[str, Any]:
        return {
            "target_type": "experiment",
            "association_role": "report",
            "template": "skills/research-workflow/report-template.md",
            "guidance": (
                "Write a SHORT markdown results report in the experiment folder "
                f"after retaining sandbox outputs, i.e. {folder}report.md. "
                "Required sections: Summary; Results — MUST contain a markdown "
                "table of metrics (paper/target value vs achieved, per task/seed "
                "where relevant); Deviations from plan ('none' if faithful); "
                "Conclusion — apply the plan's pre-registered decision rule "
                "explicitly. Keep it under 16 KB: link raw metrics files instead "
                "of inlining data. Reference figures with relative markdown image "
                "links (e.g. ![loss](figures/loss.png)); every linked image must "
                "exist in the retained local report folder before submit_results. "
                "Then register the report and associate it with role 'report'."
            ),
        }

    def _graph_resource_guidance(self, *, folder: str) -> dict[str, Any]:
        return {
            "target_type": "experiment",
            "association_role": "graph",
            "template": "skills/research-workflow/graph-template.md",
            "guidance": (
                "Write the experiment's logic graph as one JSON file in the "
                f"experiment folder ({folder}graph.json): a qualitative story "
                "you author about the logical path of the experiment — the "
                "critical questions that needed answers, the hard decisions "
                "and the reasoning behind them, the pivots (including those "
                "forced by reviews), and the lessons — told as a DAG of at "
                "most 16 nodes. Events may appear as anchors for reasoning, "
                "but this is not an event or pipeline diagram: if your nodes "
                "are components and your edges read produces/contains/records, "
                "you have drawn dataflow, not the story. Do not generate it "
                "with a script over your result files — choosing what mattered "
                "is the authorship; write the JSON yourself. You design the "
                "graph: node 'kind' vocabulary, edge labels, and structure are "
                "yours. If the graph is at the 16-node budget and something "
                "important must be added, reduce the graph to make room. Then "
                "register the file and associate it with role 'graph'."
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
        self,
        *,
        sandbox: dict[str, Any],
        experiment: dict[str, Any] | None,
        experiments: list[dict[str, Any]] | None = None,
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
        if experiments:
            process["active_experiments"] = [
                {
                    "id": item["id"],
                    "intent": item["intent"],
                    "status": item["status"],
                    "attempt_index": item["attempt_index"],
                }
                for item in experiments
            ]
        return process
