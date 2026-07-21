"""Pure next-action policy over captured Research and Sandbox facts."""

from __future__ import annotations

from typing import Any

from merv.shared.artifact_roles import (
    PROJECT_GRAPH_ROLE,
    PROJECT_GRAPH_ROLES,
    REFLECTION_LENS_DOC_ROLE,
    RESOURCE_ROLES,
)

from .domain.gates import ReviewRequirement, RoleRequirement
from .domain.paths import experiment_folder_rel
from .domain.reflection_policy import (
    idle_reflection_hint,
    reflection_create_block_reason,
)
from .domain.reflection_gates import REFLECTION_GATE_TABLE
from .domain.workflow_gates import (
    ACTIVE_PROCESS_STATUSES,
    GATE_TABLE,
    TERMINAL_STATUSES,
)
from .facade import ReviewGateSnapshot

_SLIM_RESOURCE_FIELDS = (
    "id",
    "association_role",
    "association_version_id",
    "path",
    "kind",
    "missing",
    "size_bytes",
)
_SLIM_REVIEW_FIELDS = ("id", "role", "verdict", "created_at", "synopsis")


class NextActionPolicy:
    """Compute guidance from immutable facts without reads or side effects."""

    def __init__(
        self,
        *,
        storage_enabled: bool = False,
        storage_guidance: dict[str, Any] | None = None,
    ) -> None:
        self.storage_enabled = bool(storage_enabled)
        # Composition-injected storage guidance block (object_storage prose).
        # The workflow embeds the dict it is handed instead of importing the
        # storage module; compositions pass storage_guidance(enabled=...).
        self.storage_guidance = dict(
            storage_guidance or {"enabled": self.storage_enabled}
        )

    def project_setup(self) -> dict[str, Any]:
        return self._next(
            gate="project_setup",
            action="create_claim_or_experiment",
            allowed=["claim.create", "experiment.create"],
        )

    def experiment(
        self,
        *,
        experiment: dict[str, Any],
        sandboxes: list[dict[str, Any]],
        review_gates: dict[tuple[str, str, str], ReviewGateSnapshot],
    ) -> dict[str, Any]:
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
                target_type="experiment",
                target=experiment,
                review=forward.review,
                gate=review_gates[
                    ("experiment", str(experiment["id"]), forward.review.role)
                ],
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
                    experiment=experiment,
                    requirement=requirement,
                    sandboxes=sandboxes,
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
            checklist = experiment.get("gate_checklist") or {}
            item = next(
                (
                    entry
                    for entry in checklist.get("items", [])
                    if entry.get("kind") == "resource"
                    and entry.get("role") == requirement.role
                ),
                {},
            )
            problems = list(item.get("problems") or [])
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
            revision=(
                experiment.get("revision_context", "")
                if status != "ready_to_run"
                else ""
            ),
        )

    def _requirement_gate(
        self,
        *,
        experiment: dict[str, Any],
        requirement: RoleRequirement,
        sandboxes: list[dict[str, Any]],
    ) -> str:
        """A requirement's gate name, with the one dynamic case: the running
        status's execution gate reflects whether a sandbox is actually live."""
        if experiment["status"] == "running" and requirement.gate == "execution_ready":
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
        target_type: str,
        target: dict[str, Any],
        review: ReviewRequirement,
        gate: ReviewGateSnapshot,
    ) -> dict[str, Any]:
        target_id = target["id"]
        status = target["status"]
        role, skill, action_name = review.role, review.skill, review.action_name
        transition_tool = (
            "reflection.transition"
            if target_type == "reflection"
            else "experiment.transition"
        )
        if gate.satisfied:
            return self._next(
                gate=f"{action_name}_passed",
                action=review.pass_action,
                allowed=[transition_tool],
            )

        request = gate.request
        if request is None:
            # An attested-only pass under require_verified_reviews lands here
            # (its request is already submitted): say why the gate still blocks.
            blocked_reason = gate.blocked_reason
            review_status = "attested_blocked" if blocked_reason else "none"
            action = f"launch_{action_name}er"
            allowed = ["review.request"]
            missing = [blocked_reason] if blocked_reason else []
        elif request["status"] == "requested":
            review_status = "requested"
            action = f"launch_{action_name}er"
            allowed = ["workflow.status_and_next", "review.request"]
            missing = []
        else:
            review_status = str(request["status"])
            action = f"wait_for_{action_name}"
            allowed = ["workflow.status_and_next"]
            missing = []

        return self._next(
            gate=status,
            action=action,
            allowed=allowed,
            missing=missing,
            review_gate=self._review_gate(
                role=role,
                skill=skill,
                status=review_status,
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
            "target_type": target_type,
            "target_id": target_id,
            "status": status,
            "label": labels.get(status, status),
            "read_only": True,
        }
        if request:
            gate["request_id"] = request["id"]
            gate["expires_at"] = request["expires_at"]
        return gate

    def project_reflection(
        self,
        *,
        open_wave: dict[str, Any] | None,
        signal: dict[str, Any],
        idle: bool,
        review_gates: dict[tuple[str, str, str], ReviewGateSnapshot],
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
        if open_wave is not None:
            workflow = self._reflection_workflow_for(
                reflection=open_wave, review_gates=review_gates
            )
            if signal.get("experiment_create_blocked"):
                workflow = self._with_experiment_create_block(
                    workflow=workflow, signal=signal
                )
            return {
                "reflection": self._slim_reflection(open_wave),
                "workflow": workflow,
                "signal": signal,
            }
        has_new_material = (
            signal["new_terminal_since_publish"] >= 1 or signal["contradicted_flip"]
        )
        recommended = idle and has_new_material
        if not signal["stale"] and not recommended:
            return None
        block: dict[str, Any] = {
            "reflection": None,
            "hint": signal["hint"] or idle_reflection_hint(signal=signal),
            "signal": signal,
            "experiment_create_blocked": bool(signal.get("experiment_create_blocked")),
        }
        if recommended:
            block["recommended"] = True
        return block

    def reflection_workflow_takeover(
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

    def live_experiments_takeover(
        self, *, exp_rows, reflection: dict[str, Any] | None
    ) -> dict[str, Any]:
        """The workflow block when the auto-resolved (newest-created)
        experiment is terminal while sibling experiments are still live.

        The project-level "what now?" call must never answer 'none' over live
        work: the slim projection drops the project's experiment list and the
        reflection takeover only covers the idle project, so this lists the
        live siblings in the guidance payload for the agent to re-orient onto
        — or start the next experiment. Explicit experiment-scoped calls
        never reach this (the resolved experiment's terminal gate stands)."""
        live = [
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "attempt_index": row["attempt_index"],
                "intent": row["intent"],
            }
            for row in exp_rows
            if str(row["status"]) not in TERMINAL_STATUSES
        ]
        signal = (reflection or {}).get("signal") or {}
        allowed = ["workflow.status_and_next"]
        blocked: list[dict[str, str]] = []
        if signal.get("experiment_create_blocked"):
            reason = (reflection or {}).get("hint") or reflection_create_block_reason(
                signal=signal
            )
            blocked.append({"action": "experiment.create", "reason": reason})
        else:
            allowed.append("experiment.create")
        return self._next(
            gate="live_experiments",
            action=(
                "tend_live_experiments (this experiment is finished; re-orient "
                "with workflow.status_and_next(experiment_id=...) on one of "
                "live_experiments, or create the next experiment)"
            ),
            allowed=allowed,
            blocked=blocked,
            live_experiments=live,
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

    def _reflection_workflow_for(
        self,
        *,
        reflection: dict[str, Any],
        review_gates: dict[tuple[str, str, str], ReviewGateSnapshot],
    ) -> dict[str, Any]:
        """Guidance for a reflection wave, derived from REFLECTION_GATE_TABLE —
        the same walk as _workflow_for, with the roster-coverage requirement
        reported per missing lens."""
        status = reflection["status"]
        forward = REFLECTION_GATE_TABLE.get(status)
        if forward is None:
            return self._next(gate="terminal", action="none", allowed=[])
        if forward.review is not None:
            return self._review_next(
                target_type="reflection",
                target=reflection,
                review=forward.review,
                gate=review_gates[
                    ("reflection", str(reflection["id"]), forward.review.role)
                ],
            )
        if status == "reflecting":
            coverage = reflection.get("reflection_coverage", {})
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
                    revision=reflection.get("revision_context", ""),
                )
            return self._next(
                gate=forward.ready_gate,
                action=forward.ready_action,
                allowed=list(forward.ready_allowed),
                revision=reflection.get("revision_context", ""),
            )
        roles = {
            res.get("association_role")
            for res in reflection.get("current_attempt_resources", [])
            if not res.get("missing")
        }
        for requirement in forward.requirements:
            if (
                requirement.role in roles
                or (requirement.role == "reflection_doc" and "synthesis_doc" in roles)
                or (
                    requirement.role == PROJECT_GRAPH_ROLE
                    and bool(set(PROJECT_GRAPH_ROLES) & roles)
                )
            ):
                continue
            return self._next(
                gate=requirement.gate,
                action=requirement.action,
                allowed=list(requirement.allowed),
                missing=[requirement.missing],
                resource_guidance=self._synthesizing_resource_guidance(
                    key=requirement.guidance_key
                ),
                revision=reflection.get("revision_context", ""),
            )
        return self._next(
            gate=forward.ready_gate,
            action=forward.ready_action,
            allowed=list(forward.ready_allowed),
            revision=reflection.get("revision_context", ""),
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
                "reflections/<syn_id>/reflections/<lens_id>.md), registers it, and "
                "associates it with role 'reflection_lens_doc' for this "
                "reflection wave. See the project-reflection skill for the lens briefs."
            ),
        }

    def _slim_reflection(self, reflection: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": reflection.get("id"),
            "title": reflection.get("title"),
            "status": reflection.get("status"),
            "attempt_index": reflection.get("attempt_index"),
            "revision_context": reflection.get("revision_context"),
            "roster": [
                {key: lens.get(key) for key in ("id", "title", "core")}
                for lens in reflection.get("roster", [])
            ],
            "reflection_coverage": reflection.get("reflection_coverage"),
            "current_attempt_resources": [
                {field: resource.get(field) for field in _SLIM_RESOURCE_FIELDS}
                for resource in reflection.get("current_attempt_resources", [])
            ],
            "reviews": [
                {field: review.get(field) for field in _SLIM_REVIEW_FIELDS}
                for review in reflection.get("reviews", [])
            ],
            "allowed_transitions": reflection.get("allowed_transitions", []),
        }

    def _synthesizing_resource_guidance(self, *, key: str) -> dict[str, Any] | None:
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
                    "image must resolve to a local file under 5 MB or "
                    "resource.register rejects the doc. Then register the file to "
                    "this reflection wave with role 'reflection_doc' in one "
                    "resource.register call."
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
            "or upload heavy files with storage.upload_file, then register "
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
                "Required sections: Summary; Results — interpret the system "
                "metrics exhibit (preview it with experiment.exhibit, reference "
                "it by name, and cite run names/ids from it — never numbers "
                "that aren't in it); Deviations from plan ('none' if faithful); "
                "Conclusion — apply the plan's pre-registered decision rule "
                "explicitly. Keep it under 16 KB: link raw metrics files instead "
                "of inlining data. Reference figures with relative markdown image "
                "links (e.g. ![loss](figures/loss.png)); every linked image must "
                "resolve to a local file under 5 MB or resource.register rejects "
                "the report, so copy figures off the sandbox first. "
                "Then register the report with role 'report' in one "
                "resource.register call."
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
        live_experiments: list[dict[str, Any]] | None = None,
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
        if live_experiments is not None:
            result["live_experiments"] = live_experiments
        return result


__all__ = ["NextActionPolicy"]
