"""Experiment state service."""

from __future__ import annotations

import json
from typing import Any

from ..utils import NotFoundError, ValidationError, WorkflowError
from ..utils import new_id
from ..state.store import StateStore, row_to_dict, rows_to_dicts
from ..utils import now_iso


TERMINAL_STATUSES = frozenset({"complete", "failed", "abandoned"})


class ExperimentService:
    def __init__(self, *, store: StateStore) -> None:
        self.store = store

    def create(
        self,
        *,
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
        intent = self._compose_intent(
            intent=intent,
            title=title,
            hypothesis=hypothesis,
            design=design,
            success_criteria=success_criteria,
            risks=risks,
        )
        tested_claim_ids = self._normalize_claim_ids(
            tested_claim_ids=tested_claim_ids,
            claim_id=claim_id,
            claim_ids=claim_ids,
        )
        if not intent.strip():
            raise ValidationError("intent is required")
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            for claim_id in tested_claim_ids or []:
                if conn.execute("SELECT id FROM claims WHERE id = ? AND project_id = ?", (claim_id, project_id)).fetchone() is None:
                    raise NotFoundError(f"claim not found: {claim_id}")
            experiment_id = new_id(prefix="exp")
            now = now_iso()
            conn.execute(
                """
                INSERT INTO experiments
                  (id, project_id, intent, status, attempt_index, revision_context, created_at, updated_at)
                VALUES (?, ?, ?, 'planned', 1, '', ?, ?)
                """,
                (experiment_id, project_id, intent.strip(), now, now),
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
                payload={"intent": intent},
            )
            return self.get_state(experiment_id=experiment_id, conn=conn)

    def _compose_intent(
        self,
        *,
        intent: str,
        title: str,
        hypothesis: str,
        design: str,
        success_criteria: str,
        risks: str,
    ) -> str:
        pieces = []
        if intent.strip():
            pieces.append(intent.strip())
        labeled = [
            ("Title", title),
            ("Hypothesis", hypothesis),
            ("Design", design),
            ("Success criteria", success_criteria),
            ("Risks", risks),
        ]
        for label, value in labeled:
            if value and value.strip():
                pieces.append(f"{label}: {value.strip()}")
        return "\n\n".join(pieces)

    def _normalize_claim_ids(
        self,
        *,
        tested_claim_ids: list[str] | str | None,
        claim_id: str | None,
        claim_ids: list[str] | str | None,
    ) -> list[str]:
        values: list[str] = []
        if isinstance(tested_claim_ids, str):
            values.append(tested_claim_ids)
        elif tested_claim_ids:
            values.extend(tested_claim_ids)
        if claim_id:
            values.append(claim_id)
        if isinstance(claim_ids, str):
            values.append(claim_ids)
        elif claim_ids:
            values.extend(claim_ids)
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("claim ids must be non-empty strings")
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

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
                ORDER BY c.created_at
                """,
                (experiment_id,),
            ).fetchall()
            data["tested_claims"] = rows_to_dicts(rows=claim_rows)
            resource_rows = conn.execute(
                """
                SELECT r.*, a.role AS association_role, a.attempt_index AS association_attempt_index,
                       a.version_id AS association_version_id
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
            review_rows = conn.execute(
                """
                SELECT * FROM reviews
                WHERE target_type = 'experiment' AND target_id = ?
                ORDER BY rowid DESC
                """,
                (experiment_id,),
            ).fetchall()
            reviews = rows_to_dicts(rows=review_rows)
            for review in reviews:
                review["findings"] = json.loads(review.pop("findings_json", "[]"))
                review["evidence"] = json.loads(review.pop("evidence_json", "{}"))
            data["reviews"] = reviews
            return data
        finally:
            if owns_conn:
                conn.close()

    def list_experiments(self, *, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT id FROM experiments WHERE project_id = ? ORDER BY created_at",
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

    def send_back_to_planned(self, *, conn, experiment_id: str, revision_context: str) -> None:
        row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"experiment not found: {experiment_id}")
        now = now_iso()
        conn.execute(
            """
            UPDATE experiments
            SET status = 'planned', attempt_index = attempt_index + 1,
                revision_context = ?, updated_at = ?
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
            payload={"revision_context": revision_context},
        )

    def _next_status(self, *, conn, experiment_id: str, status: str, transition: str) -> str:
        # Terminal states are final: no transition (not even abandon/mark_failed)
        # may move an experiment out of complete/failed/abandoned.
        if status in TERMINAL_STATUSES:
            raise WorkflowError(
                f"experiment is {status!r}; no transitions are allowed from a terminal state"
            )
        if transition == "abandon":
            return "abandoned"
        if transition == "mark_failed":
            return "failed"
        allowed = {
            ("planned", "submit_design"): "design_review",
            ("design_review", "mark_ready_to_run"): "ready_to_run",
            ("ready_to_run", "start_running"): "running",
            ("running", "submit_results"): "experiment_review",
            ("experiment_review", "complete"): "complete",
        }
        next_status = allowed.get((status, transition))
        if next_status is None:
            raise WorkflowError(f"transition {transition!r} is not allowed from {status!r}")
        if transition == "submit_design" and not self._has_resource_role(
            conn=conn,
            experiment_id=experiment_id,
            role="plan",
        ):
            raise WorkflowError("an experiment plan resource must be synced before design review")
        if transition == "mark_ready_to_run" and not self._has_passing_review(
            conn=conn,
            experiment_id=experiment_id,
            role="design_reviewer",
        ):
            raise WorkflowError("design review must pass before ready_to_run")
        if transition == "submit_results" and not self._has_resource_role(
            conn=conn,
            experiment_id=experiment_id,
            role="result",
        ):
            raise WorkflowError("result resource must be synced before experiment_review")
        if transition == "complete" and not self._has_passing_review(
            conn=conn,
            experiment_id=experiment_id,
            role="experiment_reviewer",
        ):
            raise WorkflowError("experiment review must pass before complete")
        return next_status

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

    def _has_passing_review(self, *, conn, experiment_id: str, role: str) -> bool:
        snapshot_id = self._target_snapshot_id(conn=conn, experiment_id=experiment_id)
        row = conn.execute(
            """
            SELECT 1
            FROM reviews
            WHERE target_type = 'experiment' AND target_id = ? AND role = ? AND verdict = 'pass'
              AND target_snapshot_id = ?
            LIMIT 1
            """,
            (experiment_id, role, snapshot_id),
        ).fetchone()
        return row is not None

    def _target_snapshot_id(self, *, conn, experiment_id: str) -> str:
        experiment = self.get_state(experiment_id=experiment_id, conn=conn)
        resource_tokens = [
            f"{res['id']}:{res.get('association_version_id') or res['version_token']}:{res.get('association_role', '')}:{res.get('association_attempt_index', 0)}"
            for res in experiment.get("current_attempt_resources", [])
        ]
        return "|".join(
            [
                "experiment",
                experiment["id"],
                experiment["status"],
                str(experiment["attempt_index"]),
                ",".join(sorted(resource_tokens)),
            ]
        )
