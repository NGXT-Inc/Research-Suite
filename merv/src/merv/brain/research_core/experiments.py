"""Experiment state service."""

from __future__ import annotations

import json
from typing import Any

from .domain.artifacts import plan_sections_missing, report_problems
from .domain.experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    active_experiment_cap_reached_message,
    compose_experiment_intent,
    infer_claim_status_from_conclusion,
    normalize_claim_ids,
)
from .domain.experiment_names import validate_experiment_name
from .domain.gates import RequirementState, ReviewState, decide_gated_transition
from .domain.graph_lint import graph_problems
from .domain.paths import experiment_folder_rel
from .domain.reflection_policy import (
    covered_terminal_ids,
    reflection_create_block_message,
)
from .domain.review_snapshot import review_snapshot_id
from .domain.workflow_gates import (
    GATE_TABLE,
    TERMINAL_STATUSES,
    allowed_transitions_for,
)
from ..artifacts.markdown_images import markdown_image_links
from ..artifacts.pinned import PinnedStore
from ..artifacts.roles import EXHIBIT_ROLE
from ..kernel.state.store import BaseStateStore, row_to_dict, rows_to_dicts
from ..kernel.utils import NotFoundError, ValidationError, WorkflowError
from ..kernel.utils import new_id
from ..kernel.utils import now_iso
from .review_gate import review_gate_state


class ExperimentService:
    def __init__(
        self,
        *,
        store: BaseStateStore,
        pinned: PinnedStore | None = None,
        storage_objects_reader: Any = None,
    ) -> None:
        self.store = store
        # Gate lints read submitted (pinned) bytes from here, never the
        # working tree. Optional only for direct construction in tests; the
        # composition root always injects it.
        self.pinned = pinned
        # Object-storage-owned query, injected at composition — research_core
        # has no import (or SQL) edge to object_storage.
        self.storage_objects_reader = storage_objects_reader

    def create(
        self,
        *,
        name: str = "",
        intent: str = "",
        tested_claim_ids: list[str] | str | None = None,
        claim_id: str | None = None,
        claim_ids: list[str] | str | None = None,
        title: str = "",
        hypothesis: str = "",
        design: str = "",
        success_criteria: str = "",
        risks: str = "",
        status: str = "planned",
        project_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if extra:
            raise ValidationError(f"unexpected experiment.create fields: {', '.join(sorted(extra))}")
        if status and status != "planned":
            raise ValidationError("experiment.create only supports status='planned'; use experiment.transition for workflow changes")
        intent = compose_experiment_intent(
            intent=intent,
            title=title,
            hypothesis=hypothesis,
            design=design,
            success_criteria=success_criteria,
            risks=risks,
        )
        tested_claim_ids = normalize_claim_ids(
            tested_claim_ids=tested_claim_ids,
            claim_id=claim_id,
            claim_ids=claim_ids,
        )
        name = validate_experiment_name(name)
        if not intent.strip():
            raise ValidationError("intent is required")
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            self._reject_active_experiment_cap(conn=conn, project_id=project_id)
            self._reject_reflection_blocked_experiment_create(
                conn=conn, project_id=project_id
            )
            duplicate = conn.execute(
                "SELECT id FROM experiments WHERE project_id = ? AND lower(name) = lower(?)",
                (project_id, name),
            ).fetchone()
            if duplicate is not None:
                raise ValidationError(
                    f"an experiment named {name!r} already exists in this project "
                    "— choose a new name"
                )
            for claim_id in tested_claim_ids or []:
                if conn.execute("SELECT id FROM claims WHERE id = ? AND project_id = ?", (claim_id, project_id)).fetchone() is None:
                    raise NotFoundError(f"claim not found: {claim_id}")
            experiment_id = new_id(prefix="exp")
            now = now_iso()
            conn.execute(
                """
                INSERT INTO experiments
                  (id, project_id, name, intent, status, attempt_index, revision_context, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'planned', 1, '', ?, ?)
                """,
                (experiment_id, project_id, name, intent.strip(), now, now),
            )
            for claim_id in tested_claim_ids or []:
                conn.execute(
                    "INSERT INTO experiment_claims (experiment_id, claim_id) VALUES (?, ?)",
                    (experiment_id, claim_id),
                )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="experiment.created",
                target_type="experiment",
                target_id=experiment_id,
                payload={"name": name, "intent": intent},
            )
            state = self.get_state(experiment_id=experiment_id, conn=conn)
            state["folder"] = experiment_folder_rel(experiment_id=experiment_id, name=name)
            state["folder_guidance"] = (
                f"Use {state['folder']} as the experiment's one local folder. "
                "Data-plane actions create it on demand; work in it from the "
                "start: plan.md, scripts, configs, retained results, report, "
                "and graph all live there. This local folder is not uploaded to "
                "a sandbox automatically: create, fetch, or explicitly transfer "
                "sandbox inputs after provisioning. Pull selected light outputs "
                "back with sandbox.pull_outputs, or upload heavy outputs to "
                "configured object storage, before the sandbox is released."
            )
            return state

    def _active_experiment_count(self, *, conn, project_id: str) -> int:
        terminal = ", ".join(f"'{status}'" for status in sorted(TERMINAL_STATUSES))
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count FROM experiments
            WHERE project_id = ? AND status NOT IN ({terminal})
            """,
            (project_id,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def _reject_active_experiment_cap(self, *, conn, project_id: str) -> None:
        active_count = self._active_experiment_count(conn=conn, project_id=project_id)
        if active_count >= ACTIVE_EXPERIMENT_CAP:
            raise WorkflowError(
                active_experiment_cap_reached_message(active_count=active_count)
            )

    def _reject_reflection_blocked_experiment_create(self, *, conn, project_id: str) -> None:
        debt, published_id = self._terminal_experiments_since_last_reflection(
            conn=conn, project_id=project_id
        )
        open_wave = conn.execute(
            """
            SELECT id, status FROM reflections
            WHERE project_id = ? AND status NOT IN ('published', 'abandoned')
            ORDER BY created_seq DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        message = reflection_create_block_message(
            debt=debt,
            published_id=published_id,
            open_wave=row_to_dict(row=open_wave),
        )
        if message:
            raise WorkflowError(message)

    def create_from_reflection(
        self,
        *,
        conn,
        project_id: str,
        reflection_id: str,
        name: str,
        intent: str,
        claim_ids: list[str],
        proposal_key: str,
        parallelism: str,
    ) -> str:
        name = validate_experiment_name(name)
        intent = intent.strip()
        self._reject_active_experiment_cap(conn=conn, project_id=project_id)
        experiment_id = new_id(prefix="exp")
        now = now_iso()
        conn.execute(
            """
            INSERT INTO experiments
              (id, project_id, name, intent, status, attempt_index,
               revision_context, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'planned', 1, '', ?, ?)
            """,
            (experiment_id, project_id, name, intent, now, now),
        )
        for claim_id in claim_ids:
            conn.execute(
                "INSERT INTO experiment_claims (experiment_id, claim_id) VALUES (?, ?)",
                (experiment_id, claim_id),
            )
        self.store.record_event(
            conn=conn,
            project_id=project_id,
            event_type="experiment.created",
            target_type="experiment",
            target_id=experiment_id,
            payload={
                "name": name,
                "intent": intent,
                "source_reflection_id": reflection_id,
                "proposal_key": proposal_key.strip(),
                "parallelism": parallelism.strip(),
            },
        )
        return experiment_id

    def _terminal_experiments_since_last_reflection(
        self, *, conn, project_id: str
    ) -> tuple[int, str | None]:
        terminal = ", ".join(f"'{status}'" for status in sorted(TERMINAL_STATUSES))
        current_terminal = {
            str(row["id"])
            for row in conn.execute(
                f"""
                SELECT id FROM experiments
                WHERE project_id = ? AND status IN ({terminal})
                """,
                (project_id,),
            ).fetchall()
        }
        published = conn.execute(
            """
            SELECT id, corpus_json FROM reflections
            WHERE project_id = ? AND status = 'published'
            ORDER BY published_at DESC, created_seq DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if published is None:
            return len(current_terminal), None
        try:
            corpus = json.loads(str(published["corpus_json"] or "{}"))
        except json.JSONDecodeError:
            corpus = {}
        covered = covered_terminal_ids(corpus)
        return len(current_terminal - covered), str(published["id"])

    def get_state(self, *, experiment_id: str, project_id: str | None = None, conn=None) -> dict[str, Any]:
        owns_conn = conn is None
        if conn is None:
            conn = self.store.connect()
        try:
            if owns_conn:
                project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"experiment not found: {experiment_id}")
            data = row_to_dict(row=row) or {}
            if project_id is not None and data["project_id"] != project_id:
                raise NotFoundError(f"experiment not found in project {project_id}: {experiment_id}")
            claim_rows = conn.execute(
                """
                SELECT c.*
                FROM claims c
                JOIN experiment_claims ec ON ec.claim_id = c.id
                WHERE ec.experiment_id = ?
                ORDER BY c.created_at, c.id
                """,
                (experiment_id,),
            ).fetchall()
            data["tested_claims"] = rows_to_dicts(rows=claim_rows)
            resource_rows = conn.execute(
                """
                SELECT r.*, a.role AS association_role, a.attempt_index AS association_attempt_index,
                       a.version_id AS association_version_id, a.created_seq AS association_rowid
                FROM resources r
                JOIN resource_associations a ON a.resource_id = r.id
                WHERE a.target_type = 'experiment' AND a.target_id = ?
                ORDER BY a.attempt_index, a.role, r.path
                """,
                (experiment_id,),
            ).fetchall()
            data["resources"] = rows_to_dicts(rows=resource_rows)
            data["current_attempt_resources"] = [
                res for res in data["resources"] if res.get("association_attempt_index") == data["attempt_index"]
            ]
            data["storage_objects"] = (
                self.storage_objects_reader(
                    conn=conn,
                    project_id=str(data["project_id"]),
                    experiment_id=experiment_id,
                )
                if self.storage_objects_reader is not None
                else []
            )
            data["mlflow_run"] = self._mlflow_run_from_row(experiment=data)
            review_rows = conn.execute(
                """
                SELECT * FROM reviews
                WHERE target_type = 'experiment' AND target_id = ?
                ORDER BY created_seq DESC
                """,
                (experiment_id,),
            ).fetchall()
            reviews = rows_to_dicts(rows=review_rows)
            for review in reviews:
                review["findings"] = json.loads(review.pop("findings_json", "[]"))
                review["evidence"] = json.loads(review.pop("evidence_json", "{}"))
            data["reviews"] = reviews
            data["allowed_transitions"] = allowed_transitions_for(str(data.get("status", "")))
            data["gate_checklist"] = self._gate_checklist(conn=conn, experiment=data)
            data["claim_update_suggestions"] = self._claim_update_suggestions(
                experiment=data
            )
            return data
        finally:
            if owns_conn:
                conn.close()

    def _mlflow_run_from_row(self, *, experiment: dict[str, Any]) -> dict[str, Any] | None:
        run_id = str(experiment.get("mlflow_run_id") or "")
        error = str(experiment.get("mlflow_run_error") or "")
        if not run_id and not error:
            return None
        result: dict[str, Any] = {
            "run_id": run_id or None,
            "run_name": str(experiment.get("mlflow_run_name") or ""),
            "status": str(experiment.get("mlflow_run_status") or ""),
            "artifact_uri": str(experiment.get("mlflow_run_artifact_uri") or ""),
            "created_at": experiment.get("mlflow_run_created_at"),
            "created_by_plugin": bool(run_id),
        }
        if error:
            result["error"] = error
        return result

    def record_mlflow_run(
        self,
        *,
        project_id: str | None = None,
        experiment_id: str,
        run: dict[str, Any],
        event_type: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            existing = self.get_state(
                experiment_id=experiment_id,
                project_id=project_id,
                conn=conn,
            )
            now = now_iso()
            run_id = str(run.get("run_id") or "")
            run_name = str(run.get("run_name") or "")
            status = str(run.get("status") or "")
            artifact_uri = str(run.get("artifact_uri") or "")
            created_at = str(run.get("created_at") or "") or now
            error = str(run.get("error") or run.get("note") or "")
            if not run_id and not error:
                return existing
            if not run_id and str(existing.get("mlflow_run_id") or ""):
                # An error-only update (e.g. a failed re-create on retry) must
                # not blank an existing run identity — keep the run, attach
                # the error beside it.
                conn.execute(
                    "UPDATE experiments SET mlflow_run_error = ?, updated_at = ? WHERE id = ?",
                    (error, now, experiment_id),
                )
                self.store.record_event(
                    conn=conn,
                    project_id=project_id,
                    event_type=event_type or "experiment.mlflow_run_unavailable",
                    target_type="experiment",
                    target_id=experiment_id,
                    payload={
                        "run_id": str(existing.get("mlflow_run_id") or ""),
                        "error": error,
                        "previous_run_id": str(existing.get("mlflow_run_id") or ""),
                    },
                )
                return self.get_state(experiment_id=experiment_id, conn=conn)
            conn.execute(
                """
                UPDATE experiments
                SET mlflow_run_id = ?,
                    mlflow_run_name = ?,
                    mlflow_run_status = ?,
                    mlflow_run_artifact_uri = ?,
                    mlflow_run_created_at = ?,
                    mlflow_run_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    run_id,
                    run_name,
                    status,
                    artifact_uri,
                    created_at if (run_id or error) else None,
                    "" if run_id else error,
                    now,
                    experiment_id,
                ),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type=(
                    event_type
                    or (
                        "experiment.mlflow_run_created"
                        if run_id
                        else "experiment.mlflow_run_unavailable"
                    )
                ),
                target_type="experiment",
                target_id=experiment_id,
                payload={
                    "run_id": run_id,
                    "run_name": run_name,
                    "status": status,
                    "error": "" if run_id else error,
                    "previous_run_id": existing.get("mlflow_run_id") or "",
                },
            )
            return self.get_state(experiment_id=experiment_id, conn=conn)

    def _claim_update_suggestions(
        self, *, experiment: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if experiment.get("status") != "complete":
            return []
        conclusion = str(experiment.get("conclusion") or "").strip()
        if not conclusion:
            return []
        suggested_status = infer_claim_status_from_conclusion(conclusion)
        # No inferable direction → no prefilled tool call. A claim.update
        # skeleton with nothing to update is malformed guidance.
        if suggested_status is None:
            return []
        suggestions: list[dict[str, Any]] = []
        for claim in experiment.get("tested_claims") or []:
            claim_id = str(claim.get("id") or "")
            if not claim_id:
                continue
            if str(claim.get("status") or "") == suggested_status:
                # Already applied (or already true) — resurfacing an
                # actionable update forever invites double-application.
                continue
            suggestions.append(
                {
                    "tool": "claim.update",
                    "arguments": {
                        "project_id": experiment.get("project_id"),
                        "claim_id": claim_id,
                        "status": suggested_status,
                    },
                    "claim": {
                        "id": claim_id,
                        "statement": claim.get("statement"),
                        "status": claim.get("status"),
                        "confidence": claim.get("confidence"),
                        "scope": claim.get("scope"),
                    },
                    "suggested_status": suggested_status,
                    "reason": (
                        "Experiment completed with a passing review; apply a "
                        "scoped claim.update if this conclusion changes the "
                        "claim's standing."
                    ),
                    "conclusion": conclusion,
                    "requires_confirmation": True,
                }
            )
        return suggestions

    def _gate_checklist(self, *, conn, experiment: dict[str, Any]) -> dict[str, Any]:
        """Current forward gate as machine-readable checklist data.

        This mirrors the declarative gate table and uses the same pinned-byte
        validators as transitions, so experiment state can show both missing
        artifacts and submitted artifact lint failures before the caller tries
        the transition.
        """
        status = str(experiment.get("status") or "")
        forward = GATE_TABLE.get(status)
        if forward is None:
            return {
                "status": status,
                "transition": None,
                "leads_to": None,
                "ready": status in TERMINAL_STATUSES,
                "items": [],
            }

        resources = experiment.get("current_attempt_resources") or []
        present_roles = {
            str(res.get("association_role"))
            for res in resources
            if res.get("association_role") and not res.get("missing")
        }
        items: list[dict[str, Any]] = []
        for requirement in forward.requirements:
            present = requirement.role in present_roles
            problems: list[str] = []
            state = "present" if present else "missing"
            if present and requirement.validator:
                problems = self.validator_problems(
                    conn=conn,
                    experiment_id=str(experiment["id"]),
                    name=requirement.validator,
                )
                state = "invalid" if problems else "valid"
            item: dict[str, Any] = {
                "id": f"resource:{requirement.role}",
                "kind": "resource",
                "role": requirement.role,
                "label": self._gate_resource_label(role=requirement.role),
                "satisfied": present and not problems,
                "status": state,
                "gate": requirement.gate,
                "action": requirement.action,
            }
            if requirement.validator:
                item["validator"] = requirement.validator
            if not present:
                item["missing"] = requirement.missing or f"{requirement.role} resource"
            if problems:
                item["problems"] = problems
            items.append(item)

        if forward.review is not None:
            review = forward.review
            snapshot_id = review_snapshot_id(target_type="experiment", target=experiment)
            gate_state = review_gate_state(
                conn=conn,
                project_id=str(experiment["project_id"]),
                target_type="experiment",
                target_id=str(experiment["id"]),
                role=review.role,
                snapshot_id=snapshot_id,
            )
            passed = gate_state["satisfied"]
            request = self._latest_review_request(
                conn=conn,
                experiment_id=str(experiment["id"]),
                role=review.role,
                target_snapshot_id=snapshot_id,
            )
            review_status = "passed" if passed else self._review_gate_status(request=request)
            item = {
                "id": f"review:{review.role}",
                "kind": "review",
                "role": review.role,
                "label": self._gate_review_label(role=review.role),
                "satisfied": passed,
                "status": review_status,
                "gate": status,
                "action": review.pass_action if passed else f"launch_{review.action_name}er",
                "skill": review.skill,
            }
            if gate_state.get("blocked_reason"):
                item["problems"] = [gate_state["blocked_reason"]]
            if request is not None:
                item["request_id"] = request["id"]
                item["expires_at"] = request["expires_at"]
            items.append(item)

        return {
            "status": status,
            "transition": forward.name,
            "leads_to": forward.to_status,
            "ready": all(bool(item.get("satisfied")) for item in items),
            "items": items,
        }

    def _latest_review_request(
        self,
        *,
        conn,
        experiment_id: str,
        role: str,
        target_snapshot_id: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT id, status, expires_at
            FROM review_requests
            WHERE target_type = 'experiment' AND target_id = ? AND role = ?
              AND target_snapshot_id = ?
            ORDER BY created_seq DESC
            LIMIT 1
            """,
            (experiment_id, role, target_snapshot_id),
        ).fetchone()
        return row_to_dict(row=row)

    def _review_gate_status(self, *, request: dict[str, Any] | None) -> str:
        if request is None:
            return "pending"
        if request.get("status") in {"requested", "started"}:
            return str(request["status"])
        return "pending"

    def _gate_resource_label(self, *, role: str) -> str:
        labels = {
            "plan": "Plan associated and valid",
            "result": "Result resource present",
            "report": "Results report present and valid",
            "graph": "Logic graph present and valid",
        }
        return labels.get(role, f"{role} resource present")

    def _gate_review_label(self, *, role: str) -> str:
        labels = {
            "design_reviewer": "Design review passed",
            "experiment_reviewer": "Experiment review passed",
        }
        return labels.get(role, f"{role} review passed")

    def list_experiments(self, *, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT id FROM experiments WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return {"experiments": [self.get_state(experiment_id=row["id"], conn=conn) for row in rows]}
        finally:
            conn.close()

    def transition(
        self,
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            experiment = self.get_state(experiment_id=experiment_id, project_id=project_id, conn=conn)
            status = experiment["status"]
            next_status = self._next_status(
                conn=conn,
                experiment_id=experiment_id,
                status=status,
                transition=transition,
            )
            now = now_iso()
            if transition == "complete":
                conn.execute(
                    "UPDATE experiments SET status = ?, conclusion = ?, updated_at = ? WHERE id = ?",
                    (next_status, self._conclusion_from_evidence(evidence), now, experiment_id),
                )
            elif transition == "retry_running":
                revision_context = self._retry_running_context(
                    evidence=evidence,
                    previous=str(experiment.get("revision_context") or ""),
                )
                conn.execute(
                    "UPDATE experiments SET status = ?, revision_context = ?, updated_at = ? WHERE id = ?",
                    (next_status, revision_context, now, experiment_id),
                )
            else:
                conn.execute(
                    "UPDATE experiments SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status, now, experiment_id),
                )
            self.store.record_event(
                conn=conn,
                project_id=experiment["project_id"],
                event_type="experiment.transitioned",
                target_type="experiment",
                target_id=experiment_id,
                payload={"from": status, "to": next_status, "transition": transition, "evidence": evidence or {}},
            )
            return self.get_state(experiment_id=experiment_id, conn=conn)

    def _conclusion_from_evidence(self, evidence: dict[str, Any] | None) -> str:
        """Derive the durable conclusion text persisted when an experiment
        completes. Prefer an explicit `conclusion` string; otherwise serialize
        the whole evidence object so the accepted reasoning is not lost."""
        if not evidence:
            return ""
        conclusion = evidence.get("conclusion")
        if isinstance(conclusion, str) and conclusion.strip():
            return conclusion.strip()
        return json.dumps(evidence, sort_keys=True)

    def _retry_running_context(
        self, *, evidence: dict[str, Any] | None, previous: str = ""
    ) -> str:
        evidence = evidence or {}
        reason = str(evidence.get("reason") or "infrastructure failure").strip()
        detail = str(
            evidence.get("detail")
            or evidence.get("notes")
            or evidence.get("note")
            or ""
        ).strip()
        parts = [
            "Infrastructure retry requested while experiment was running.",
            "Approved plan and current attempt stay in force; rerun execution and retain fresh results before submit_results.",
            f"Reason: {reason}.",
        ]
        if detail:
            parts.append(f"Detail: {detail}")
        context = " ".join(parts)
        return f"{previous}\n\n{context}".strip() if previous else context

    def send_back_to_planned(self, *, conn, experiment_id: str, revision_context: str) -> None:
        row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"experiment not found: {experiment_id}")
        if row["status"] not in {"design_review", "experiment_review"}:
            raise WorkflowError(
                f"experiment is {row['status']!r}; only an experiment under "
                "review can be sent back to planned"
            )
        now = now_iso()
        # Run identity is per-attempt: clear the persisted MLflow run so the
        # revised attempt's start_running mints a fresh one instead of telling
        # the agent to resume the previous attempt's (usually finalized) run.
        conn.execute(
            """
            UPDATE experiments
            SET status = 'planned', attempt_index = attempt_index + 1,
                revision_context = ?, updated_at = ?,
                mlflow_run_id = '', mlflow_run_name = '', mlflow_run_status = '',
                mlflow_run_artifact_uri = '', mlflow_run_created_at = NULL,
                mlflow_run_error = ''
            WHERE id = ?
            """,
            (revision_context, now, experiment_id),
        )
        self.store.record_event(
            conn=conn,
            project_id=row["project_id"],
            event_type="experiment.returned_to_planned",
            target_type="experiment",
            target_id=experiment_id,
            payload={
                "revision_context": revision_context,
                "previous_mlflow_run_id": str(row["mlflow_run_id"] or ""),
            },
        )

    def send_back_to_running(self, *, conn, experiment_id: str, revision_context: str) -> None:
        """Reject an executed attempt back to execution: the approved plan and
        its attempt-scoped resources stay valid, so attempt_index is NOT bumped
        — only execution and/or the conclusion must be redone before results
        are resubmitted."""
        row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"experiment not found: {experiment_id}")
        if row["status"] != "experiment_review":
            raise WorkflowError(
                f"experiment is {row['status']!r}; only an experiment under "
                "experiment_review can be sent back to running"
            )
        now = now_iso()
        conn.execute(
            "UPDATE experiments SET status = 'running', revision_context = ?, updated_at = ? WHERE id = ?",
            (revision_context, now, experiment_id),
        )
        self.store.record_event(
            conn=conn,
            project_id=row["project_id"],
            event_type="experiment.returned_to_running",
            target_type="experiment",
            target_id=experiment_id,
            payload={"revision_context": revision_context},
        )

    def _next_status(self, *, conn, experiment_id: str, status: str, transition: str) -> str:
        forward = GATE_TABLE.get(status)
        requirement_states: list[RequirementState] = []
        review_state: ReviewState | None = None
        if forward is not None:
            for requirement in forward.requirements:
                validation_error = ""
                present = self._has_resource_role(
                    conn=conn,
                    experiment_id=experiment_id,
                    role=requirement.role,
                )
                if present and requirement.validator:
                    try:
                        self._run_validator(
                            conn=conn,
                            experiment_id=experiment_id,
                            name=requirement.validator,
                        )
                    except WorkflowError as exc:
                        validation_error = str(exc)
                requirement_states.append(
                    RequirementState(
                        role=requirement.role,
                        present=present,
                        missing_error=requirement.error,
                        validation_error=validation_error,
                    )
                )
            if forward.review is not None:
                gate_state = self._review_gate_state(
                    conn=conn, experiment_id=experiment_id, role=forward.review.role
                )
                review_state = ReviewState(
                    satisfied=bool(gate_state["satisfied"]),
                    error=forward.review.error,
                    blocked_reason=str(gate_state.get("blocked_reason") or ""),
                )
        direct_transitions = {"abandon": "abandoned", "mark_failed": "failed"}
        if status == "running":
            direct_transitions["retry_running"] = "running"
        return decide_gated_transition(
            subject="experiment",
            status=status,
            transition=transition,
            terminal_statuses=TERMINAL_STATUSES,
            direct_transitions=direct_transitions,
            forward=forward,
            requirement_states=requirement_states,
            review_state=review_state,
            allowed_transitions=allowed_transitions_for(status),
        )

    def _run_validator(self, *, conn, experiment_id: str, name: str) -> None:
        """Dispatch a gate-table validator name to its deep-lint implementation."""
        if name == "plan":
            self._validate_plan_sections(conn=conn, experiment_id=experiment_id)
        elif name == "report":
            self._validate_results_report(conn=conn, experiment_id=experiment_id)
        elif name == "graph":
            self._validate_logic_graph(conn=conn, experiment_id=experiment_id)

    def validator_problems(self, *, conn, experiment_id: str, name: str) -> list[str]:
        """A gate-table validator's findings as data instead of a raise.

        Runs the exact same deep lint the transition runs, so the workflow's
        readiness guidance can never call an artifact ready that the
        transition would reject."""
        try:
            self._run_validator(conn=conn, experiment_id=experiment_id, name=name)
        except WorkflowError as exc:
            return [str(exc)]
        return []

    def _has_resource_role(self, *, conn, experiment_id: str, role: str) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM resource_associations
            WHERE target_type = 'experiment' AND target_id = ? AND role = ?
              AND attempt_index = (SELECT attempt_index FROM experiments WHERE id = ?)
            LIMIT 1
            """,
            (experiment_id, role, experiment_id),
        ).fetchone()
        return row is not None

    def _pinned_text(
        self, *, conn, experiment_id: str, role: str, what: str
    ) -> tuple[str, str, str]:
        """(text, version_id, path) of the current attempt's submitted artifact.

        Gates lint the bytes pinned at resource.register — never the working
        tree — so fixing an artifact means fix the file AND re-register it.
        """
        if self.pinned is None:
            raise WorkflowError(
                f"{what}: no blob store is configured; gated artifacts cannot be linted"
            )
        attempt = conn.execute(
            "SELECT attempt_index FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if attempt is None:
            raise NotFoundError(f"experiment not found: {experiment_id}")
        return self.pinned.artifact_text(
            conn=conn,
            target_type="experiment",
            target_id=experiment_id,
            role=role,
            attempt_index=int(attempt["attempt_index"]),
            what=what,
        )

    def _validate_plan_sections(self, *, conn, experiment_id: str) -> None:
        """Block submit_design unless the current attempt's SUBMITTED plan fills
        in the required spine and every relative figure link has submitted
        figure content. Lints the bytes pinned at associate; editing the
        live file changes nothing until it is re-associated."""
        plan_text, version_id, path = self._pinned_text(
            conn=conn,
            experiment_id=experiment_id,
            role="plan",
            what="experiment plan",
        )
        missing = plan_sections_missing(plan_text)
        if missing:
            raise WorkflowError(
                "experiment plan is missing required sections before design review: "
                + ", ".join(missing)
                + ". Fill in the plan template's required spine — Summary; "
                "Objective & hypothesis; Evaluation — then re-associate the plan "
                "to submit the fix; see skills/research-workflow/plan-template.md."
            )
        figures = {
            str(row["link_path"])
            for row in conn.execute(
                "SELECT link_path FROM report_figures WHERE report_version_id = ?",
                (version_id,),
            ).fetchall()
        }
        problems = [
            f"figure {link!r} has no submitted content: make sure the file "
            f"exists next to {path} (copy it out first if it was produced "
            "on the sandbox), then re-associate the plan to submit it"
            for link in markdown_image_links(plan_text)
            if link not in figures
        ]
        if problems:
            raise WorkflowError(
                "experiment plan is not ready for design review: "
                + "; ".join(problems)
            )

    def _validate_results_report(self, *, conn, experiment_id: str) -> None:
        """Block submit_results unless the current attempt's SUBMITTED report
        passes the report lint — including every relative figure link having
        submitted figure content (captured when the report was associated),
        and a reference to the system metrics exhibit when one is pinned for
        this attempt (quantitative attempts; the tool layer generates and pins
        it before the transition gate runs)."""
        report_text, version_id, path = self._pinned_text(
            conn=conn,
            experiment_id=experiment_id,
            role="report",
            what="results report",
        )
        figures = {
            str(row["link_path"]): row
            for row in conn.execute(
                "SELECT link_path, sha256 FROM report_figures WHERE report_version_id = ?",
                (version_id,),
            ).fetchall()
        }

        def figure_problem(link: str) -> str | None:
            if link in figures:
                return None
            return (
                f"figure {link!r} has no submitted content: make sure the file "
                f"exists next to {path} (copy it out first if it was produced "
                "on the sandbox), then re-associate the report to submit it"
            )

        exhibit = self.exhibit_association(conn=conn, experiment_id=experiment_id)
        problems = report_problems(
            report_text,
            figure_problem=figure_problem,
            exhibit_path=exhibit["path"] if exhibit else None,
        )
        if problems:
            raise WorkflowError(
                "results report is not ready for experiment review: "
                + "; ".join(problems)
                + ". Fix the file and re-associate it to submit the revision — "
                "see skills/research-workflow/report-template.md."
            )

    def _validate_logic_graph(self, *, conn, experiment_id: str) -> None:
        """Block submit_results unless the current attempt's SUBMITTED logic
        graph passes the envelope lint. The lint checks shape only (parses,
        node budget, DAG) — the story itself is the agent's to tell and the
        experiment reviewer's to judge."""
        graph_text, _, _ = self._pinned_text(
            conn=conn,
            experiment_id=experiment_id,
            role="graph",
            what="logic graph",
        )
        problems = graph_problems(graph_text)
        if problems:
            raise WorkflowError(
                "logic graph is not ready for experiment review: "
                + "; ".join(problems)
                + ". Fix the file and re-associate it to submit the revision — "
                "see skills/research-workflow/graph-template.md."
            )

    def exhibit_association(
        self, *, conn, experiment_id: str
    ) -> dict[str, Any] | None:
        """The current attempt's pinned system metrics exhibit, if any:
        {path, version_id, resource_id}. The exhibit is system-authored (the
        tool layer pins it at submit_results), so its presence — not any
        agent claim — is what the report gate keys on."""
        row = conn.execute(
            """
            SELECT r.path, a.version_id, a.resource_id
            FROM resource_associations a
            JOIN resources r ON r.id = a.resource_id
            WHERE a.target_type = 'experiment' AND a.target_id = ? AND a.role = ?
              AND a.attempt_index = (SELECT attempt_index FROM experiments WHERE id = ?)
              AND r.deleted = 0
            ORDER BY a.created_seq DESC
            LIMIT 1
            """,
            (experiment_id, EXHIBIT_ROLE, experiment_id),
        ).fetchone()
        return row_to_dict(row=row)

    def attempt_started_running_at(self, *, experiment_id: str) -> str | None:
        """When the current attempt entered running — the metrics-exhibit
        window start. Derived from the transition event stream: each attempt
        passes through start_running exactly once (retry_running and
        send_back_to_running keep the experiment running), so the latest
        start_running event belongs to the current attempt."""
        conn = self.store.connect()
        try:
            rows = conn.execute(
                """
                SELECT payload_json, created_at FROM events
                WHERE target_type = 'experiment' AND target_id = ?
                  AND type = 'experiment.transitioned'
                ORDER BY id DESC
                """,
                (experiment_id,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if payload.get("transition") == "start_running":
                return str(row["created_at"])
        return None

    def record_exhibit_verdict(
        self,
        *,
        experiment_id: str,
        verdict: dict[str, Any],
        project_id: str | None = None,
    ) -> None:
        """Persist the exhibit generation verdict to the event stream — the
        claim-validity instrumentation row (runs found, result files, pinned)
        for the gates-vs-no-gates benchmark."""
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="experiment.exhibit_generated",
                target_type="experiment",
                target_id=experiment_id,
                payload=verdict,
            )

    def _review_gate_state(self, *, conn, experiment_id: str, role: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT project_id FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        return review_gate_state(
            conn=conn,
            project_id=str(row["project_id"]) if row else "",
            target_type="experiment",
            target_id=experiment_id,
            role=role,
            snapshot_id=self._target_snapshot_id(conn=conn, experiment_id=experiment_id),
        )

    def target_snapshot_id(self, *, conn, experiment_id: str) -> str:
        return self._target_snapshot_id(conn=conn, experiment_id=experiment_id)

    def _target_snapshot_id(self, *, conn, experiment_id: str) -> str:
        experiment = self.get_state(experiment_id=experiment_id, conn=conn)
        return review_snapshot_id(target_type="experiment", target=experiment)
