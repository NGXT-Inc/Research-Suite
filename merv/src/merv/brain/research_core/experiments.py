"""Experiment state service."""

from __future__ import annotations

from contextlib import closing
import json
from typing import Any

from merv.shared.artifact_roles import EXHIBIT_ROLE
from merv.shared.markdown_images import markdown_image_links

from .domain.artifacts import plan_sections_missing, report_problems
from .domain.experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    active_experiment_cap_reached_message,
    compose_experiment_intent,
    normalize_claim_ids,
)
from .domain.experiment_names import validate_experiment_name
from .domain.graph_lint import graph_problems
from .domain.reflection_policy import (
    covered_terminal_ids,
    reflection_create_block_message,
)
from .domain.review_snapshot import review_snapshot_id
from .domain.artifact_evidence import (
    preferred_associated_artifact,
    artifact_state_record,
)
from .domain.workflow_gates import (
    GATE_TABLE,
    TERMINAL_STATUSES,
    allowed_transitions_for,
)
from .gate_evaluation import (
    GateEvaluation,
    RequirementEvaluation,
    evaluate_artifact_requirement,
)
from ..artifacts.ports import AssociatedEvidence, EvidenceReader, SubmittedDocument
from ..kernel.events import StoredEvent
from ..kernel.state.store import BaseStateStore, row_to_dict, rows_to_dicts
from ..kernel.utils import NotFoundError, ValidationError, WorkflowError
from ..kernel.utils import new_id
from ..kernel.utils import now_iso
from .review_gate import evaluate_review_gate
from .transition_types import (
    CommittedExperimentTransition,
    CommittedTrackingRunRefresh,
)


def _query(conn, sql: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
    return rows_to_dicts(rows=conn.execute(sql, parameters).fetchall())


class ExperimentService:
    def __init__(
        self,
        *,
        store: BaseStateStore,
        evidence_reader: EvidenceReader,
    ) -> None:
        self.store = store
        self.evidence_reader = evidence_reader

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
            raise ValidationError(
                f"unexpected experiment.create fields: {', '.join(sorted(extra))}"
            )
        if status and status != "planned":
            raise ValidationError(
                "experiment.create only supports status='planned'; use experiment.transition for workflow changes"
            )
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
                if (
                    conn.execute(
                        "SELECT id FROM claims WHERE id = ? AND project_id = ?",
                        (claim_id, project_id),
                    ).fetchone()
                    is None
                ):
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

    def _reject_reflection_blocked_experiment_create(
        self, *, conn, project_id: str
    ) -> None:
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

    def get_state(
        self, *, experiment_id: str, project_id: str | None = None, conn=None
    ) -> dict[str, Any]:
        return self.get_state_with_gate(
            experiment_id=experiment_id, project_id=project_id, conn=conn
        )[0]

    def get_state_with_gate(
        self, *, experiment_id: str, project_id: str | None = None, conn=None
    ) -> tuple[dict[str, Any], GateEvaluation]:
        owns_conn = conn is None
        if conn is None:
            conn = self.store.connect()
        try:
            if owns_conn:
                project_id = self.store.require_project_id(
                    conn=conn, project_id=project_id
                )
            row = conn.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"experiment not found: {experiment_id}")
            data = row_to_dict(row=row) or {}
            if project_id is not None and data["project_id"] != project_id:
                raise NotFoundError(
                    f"experiment not found in project {project_id}: {experiment_id}"
                )
            return self._assemble_state_with_gate(
                conn=conn,
                experiment=data,
                tested_claims=_query(
                    conn,
                    """
                    SELECT c.* FROM claims c
                    JOIN experiment_claims ec ON ec.claim_id = c.id
                    WHERE ec.experiment_id = ?
                    ORDER BY c.created_at, c.id
                    """,
                    (experiment_id,),
                ),
                evidence=self.evidence_reader.artifacts_for_target(
                    target_type="experiment", target_id=experiment_id
                ),
                reviews=_query(
                    conn,
                    """SELECT * FROM reviews
                    WHERE target_type = 'experiment' AND target_id = ?
                    ORDER BY created_seq DESC""",
                    (experiment_id,),
                ),
            )
        finally:
            if owns_conn:
                conn.close()

    def list_states_with_gates(
        self, *, conn, project_id: str
    ) -> list[tuple[dict[str, Any], GateEvaluation]]:
        """Hydrate a project's experiment states with one read per child table."""
        experiment_rows = _query(
            conn,
            "SELECT * FROM experiments WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        )
        experiment_ids = tuple(str(row["id"]) for row in experiment_rows)
        if not experiment_ids:
            return []

        claims: dict[str, list[dict[str, Any]]] = {}
        for claim in _query(
            conn,
            """SELECT ec.experiment_id AS _experiment_id, c.*
            FROM experiment_claims ec
            JOIN experiments e ON e.id = ec.experiment_id
            JOIN claims c ON c.id = ec.claim_id
            WHERE e.project_id = ?
            ORDER BY e.created_at, e.id, c.created_at, c.id""",
            (project_id,),
        ):
            experiment_id = str(claim.pop("_experiment_id"))
            claims.setdefault(experiment_id, []).append(claim)

        reviews: dict[str, list[dict[str, Any]]] = {}
        for review in _query(
            conn,
            """SELECT r.* FROM reviews r
            JOIN experiments e ON e.id = r.target_id
            WHERE r.target_type = 'experiment' AND e.project_id = ?
            ORDER BY e.created_at, e.id, r.created_seq DESC""",
            (project_id,),
        ):
            reviews.setdefault(str(review["target_id"]), []).append(review)

        evidence = self.evidence_reader.artifacts_for_targets(
            target_type="experiment", target_ids=experiment_ids
        )
        return [
            self._assemble_state_with_gate(
                conn=conn,
                experiment=experiment,
                tested_claims=claims.get(str(experiment["id"]), []),
                evidence=evidence.get(str(experiment["id"]), ()),
                reviews=reviews.get(str(experiment["id"]), []),
            )
            for experiment in experiment_rows
        ]

    def _assemble_state_with_gate(
        self,
        *,
        conn,
        experiment: dict[str, Any],
        tested_claims: list[dict[str, Any]],
        evidence: tuple[AssociatedEvidence, ...],
        reviews: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], GateEvaluation]:
        data = dict(experiment)
        data["tested_claims"] = tested_claims
        data["artifacts"] = [artifact_state_record(item) for item in evidence]
        data["current_attempt_artifacts"] = [
            artifact
            for artifact in data["artifacts"]
            if artifact.get("attempt_index") == data["attempt_index"]
        ]
        data["mlflow_run"] = self._mlflow_run_from_row(experiment=data)
        for review in reviews:
            review["findings"] = json.loads(review.pop("findings_json", "[]"))
            review["evidence"] = json.loads(review.pop("evidence_json", "{}"))
        data["reviews"] = reviews
        evaluation = self._evaluate_gate(conn=conn, experiment=data)
        data["allowed_transitions"] = [dict(x) for x in evaluation.legal_transitions]
        data["gate_checklist"] = evaluation.checklist()
        return data, evaluation

    def assert_in_project(self, *, experiment_id: str, project_id: str) -> None:
        """Verify experiment identity/scope without hydrating its child records."""
        with closing(self.store.connect()) as conn:
            row = conn.execute("SELECT 1 FROM experiments WHERE id = ? AND project_id = ?", (experiment_id, project_id)).fetchone()
        if row is None:
            raise NotFoundError(f"experiment not found in project {project_id}: {experiment_id}")

    def _mlflow_run_from_row(
        self, *, experiment: dict[str, Any]
    ) -> dict[str, Any] | None:
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
        return_event: bool = False,
    ) -> dict[str, Any] | CommittedTrackingRunRefresh:
        def result(
            state: dict[str, Any], event: StoredEvent
        ) -> dict[str, Any] | CommittedTrackingRunRefresh:
            return CommittedTrackingRunRefresh(state, event) if return_event else state

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
                event = self.store.record_event(
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
                state = self.get_state(experiment_id=experiment_id, conn=conn)
                return result(state, event)
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
            event = self.store.record_event(
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
            state = self.get_state(experiment_id=experiment_id, conn=conn)
            return result(state, event)

    def _evaluate_gate(self, *, conn, experiment: dict[str, Any]) -> GateEvaluation:
        """Collect current facts once for enforcement, state, and guidance."""
        status = str(experiment.get("status") or "")
        forward = GATE_TABLE.get(status)
        artifacts = experiment.get("current_attempt_artifacts") or []
        present_roles = {
            str(art.get("role"))
            for art in artifacts
            if art.get("role")
        }
        requirements: list[RequirementEvaluation] = []
        for requirement in () if forward is None else forward.requirements:
            present = requirement.role in present_roles
            problems: tuple[str, ...] = ()
            if present and requirement.validator:
                try:
                    self._run_validator(
                        experiment=experiment, name=requirement.validator
                    )
                except WorkflowError as exc:
                    problems = (str(exc),)
            requirements.append(
                evaluate_artifact_requirement(
                    requirement,
                    present=present,
                    problems=problems,
                )
            )

        review = (
            None
            if forward is None or forward.review is None
            else evaluate_review_gate(
                conn=conn,
                target_type="experiment",
                target=experiment,
                review=forward.review,
            )
        )
        return GateEvaluation(
            subject="experiment",
            status=status,
            transition=None if forward is None else forward.name,
            leads_to=None if forward is None else forward.to_status,
            terminal=status in TERMINAL_STATUSES,
            requirements=tuple(requirements),
            review=review,
            legal_transitions=tuple(
                dict(item) for item in allowed_transitions_for(status)
            ),
        )

    def list_experiments(self, *, project_id: str | None = None) -> dict[str, Any]:
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            states = self.list_states_with_gates(conn=conn, project_id=project_id)
            return {"experiments": [state for state, _gate in states]}

    def list_experiment_summaries(
        self, *, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                """
                SELECT id, project_id, name, intent, status, attempt_index,
                       created_at, updated_at
                FROM experiments
                WHERE project_id = ?
                ORDER BY created_at, id
                """,
                (project_id,),
            ).fetchall()
            return rows_to_dicts(rows=rows)

    def transition(
        self,
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        committed = self.transition_with_event(
            experiment_id=experiment_id,
            transition=transition,
            evidence=evidence,
            project_id=project_id,
        )
        return committed.state

    def transition_with_event(
        self,
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> CommittedExperimentTransition:
        """Transition atomically and expose its exact event after commit."""

        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            experiment, gate = self.get_state_with_gate(
                experiment_id=experiment_id, project_id=project_id, conn=conn
            )
            status = experiment["status"]
            next_status = gate.require_transition(transition)
            now = now_iso()
            if transition == "complete":
                conn.execute(
                    "UPDATE experiments SET status = ?, conclusion = ?, updated_at = ? WHERE id = ?",
                    (
                        next_status,
                        self._conclusion_from_evidence(evidence),
                        now,
                        experiment_id,
                    ),
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
            event = self.store.record_event(
                conn=conn,
                project_id=experiment["project_id"],
                event_type="experiment.transitioned",
                target_type="experiment",
                target_id=experiment_id,
                payload={
                    "from": status,
                    "to": next_status,
                    "transition": transition,
                    "evidence": evidence or {},
                },
            )
            state = self.get_state(experiment_id=experiment_id, conn=conn)
            return CommittedExperimentTransition(state=state, event=event)

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

    def send_back_to_planned(
        self, *, conn, experiment_id: str, revision_context: str
    ) -> None:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
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

    def send_back_to_running(
        self, *, conn, experiment_id: str, revision_context: str
    ) -> None:
        """Reject an executed attempt back to execution: the approved plan and
        its attempt-scoped artifacts stay valid, so attempt_index is NOT bumped
        — only execution and/or the conclusion must be redone before results
        are resubmitted."""
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
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

    def _run_validator(self, *, experiment: dict[str, Any], name: str) -> None:
        """Dispatch a gate-table validator name to its deep-lint implementation."""
        if name == "plan":
            self._validate_plan_sections(experiment=experiment)
        elif name == "report":
            self._validate_results_report(experiment=experiment)
        elif name == "graph":
            self._validate_logic_graph(experiment=experiment)

    def _submitted_document(
        self, *, experiment: dict[str, Any], role: str, what: str
    ) -> SubmittedDocument:
        artifact = preferred_associated_artifact(
            artifacts=experiment.get("current_attempt_artifacts") or [],
            attempt=experiment.get("attempt_index"),
            roles=(role,),
        )
        if artifact is None:
            raise WorkflowError(
                f"no {role!r} artifact is submitted for the current attempt"
            )
        return self.evidence_reader.submitted_document(
            artifact_id=str(artifact.get("id") or ""), what=what
        )

    def _validate_plan_sections(self, *, experiment: dict[str, Any]) -> None:
        """Block submit_design unless the current attempt's SUBMITTED plan fills
        in the required spine and every relative figure link has submitted
        figure content. Lints the bytes pinned at associate; editing the
        live file changes nothing until it is resubmitted."""
        document = self._submitted_document(
            experiment=experiment,
            role="plan",
            what="experiment plan",
        )
        plan_text, path = document.text, document.path
        missing = plan_sections_missing(plan_text)
        if missing:
            raise WorkflowError(
                "experiment plan is missing required sections before design review: "
                + ", ".join(missing)
                + ". Fill in the plan template's required spine — Summary; "
                "Objective & hypothesis; Evaluation — then resubmit the plan "
                "to submit the fix; see skills/research-workflow/plan-template.md."
            )
        figures = set(document.figure_links)
        problems = [
            f"figure {link!r} has no submitted content: make sure the file "
            f"exists next to {path} (copy it out first if it was produced "
            "on the sandbox), then resubmit the plan to submit it"
            for link in markdown_image_links(plan_text)
            if link not in figures
        ]
        if problems:
            raise WorkflowError(
                "experiment plan is not ready for design review: " + "; ".join(problems)
            )

    def _validate_results_report(self, *, experiment: dict[str, Any]) -> None:
        """Block submit_results unless the current attempt's SUBMITTED report
        passes the report lint — including every relative figure link having
        submitted figure content (captured when the report was associated),
        and a reference to the system metrics exhibit when one is pinned for
        this attempt (quantitative attempts; the tool layer generates and pins
        it before the transition gate runs)."""
        document = self._submitted_document(
            experiment=experiment,
            role="report",
            what="results report",
        )
        report_text, path = document.text, document.path
        figures = set(document.figure_links)

        def figure_problem(link: str) -> str | None:
            if link in figures:
                return None
            return (
                f"figure {link!r} has no submitted content: make sure the file "
                f"exists next to {path} (copy it out first if it was produced "
                "on the sandbox), then resubmit the report to submit it"
            )

        exhibit = preferred_associated_artifact(
            artifacts=experiment.get("current_attempt_artifacts") or [],
            attempt=experiment.get("attempt_index"),
            roles=(EXHIBIT_ROLE,),
        )
        problems = report_problems(
            report_text,
            figure_problem=figure_problem,
            exhibit_path=exhibit["path"] if exhibit else None,
        )
        if problems:
            raise WorkflowError(
                "results report is not ready for experiment review: "
                + "; ".join(problems)
                + ". Fix the file and resubmit it (artifact.submit) — "
                "see skills/research-workflow/report-template.md."
            )

    def _validate_logic_graph(self, *, experiment: dict[str, Any]) -> None:
        """Block submit_results unless the current attempt's SUBMITTED logic
        graph passes the envelope lint. The lint checks shape only (parses,
        node budget, DAG) — the story itself is the agent's to tell and the
        experiment reviewer's to judge."""
        document = self._submitted_document(
            experiment=experiment,
            role="graph",
            what="logic graph",
        )
        problems = graph_problems(document.text)
        if problems:
            raise WorkflowError(
                "logic graph is not ready for experiment review: "
                + "; ".join(problems)
                + ". Fix the file and resubmit it (artifact.submit) — "
                "see skills/research-workflow/graph-template.md."
            )

    def attempt_started_running_at(self, *, experiment_id: str) -> str | None:
        """When the current attempt entered running — the metrics-exhibit
        window start. Derived from the transition event stream: each attempt
        passes through start_running exactly once (retry_running and
        send_back_to_running keep the experiment running), so the latest
        start_running event belongs to the current attempt."""
        with closing(self.store.connect()) as conn:
            rows = conn.execute(
                """
                SELECT payload_json, created_at FROM events
                WHERE target_type = 'experiment' AND target_id = ?
                  AND type = 'experiment.transitioned'
                ORDER BY id DESC
                """,
                (experiment_id,),
            ).fetchall()
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

    def target_snapshot_id(self, *, conn, experiment_id: str) -> str:
        experiment = self.get_state(experiment_id=experiment_id, conn=conn)
        return review_snapshot_id(target_type="experiment", target=experiment)
