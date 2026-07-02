"""Experiment state service."""

from __future__ import annotations

import json
import re
from typing import Any

from ..domain.artifacts import plan_sections_missing, report_problems
from ..domain.experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    active_experiment_cap_reached_message,
)
from ..domain.experiment_names import validate_experiment_name
from ..domain.graph_lint import graph_problems
from ..domain.paths import experiment_folder_rel
from ..domain.reflection_policy import (
    REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
    covered_terminal_ids,
)
from ..domain.review_snapshot import review_snapshot_id
from ..domain.workflow_gates import (
    GATE_TABLE,
    TERMINAL_STATUSES,
    allowed_transitions_for,
)
from ..state.blobs import BlobStore
from ..state.store import BaseStateStore, row_to_dict, rows_to_dicts
from ..utils import NotFoundError, ValidationError, WorkflowError
from ..utils import new_id
from ..utils import now_iso
from .pinned import pinned_artifact_text

# Claim-status inference markers: (pattern, plain vote, vote when negated in
# the same clause). A None vote means the direction is unclear — inference
# bails entirely rather than guessing (see _infer_claim_status_from_conclusion).
_CLAIM_STATUS_MARKERS: tuple[tuple[re.Pattern[str], str | None, str | None], ...] = (
    # Refutation stems: "does not contradict" tells us what did NOT happen,
    # not whether the claim is supported or merely weakened.
    (re.compile(r"\bcontradict\w*"), "contradicted", None),
    (re.compile(r"\brefut\w*"), "contradicted", None),
    (re.compile(r"\bfalsif\w*"), "contradicted", None),
    (re.compile(r"\bdisprov\w*"), "contradicted", None),
    # Support stems: negated forms ("could not confirm", "did not improve")
    # are evidence against, i.e. weakened.
    (re.compile(r"\bsupport(?:s|ed|ing)?\b"), "supported", "weakened"),
    (re.compile(r"\bconfirm\w*"), "supported", "weakened"),
    (re.compile(r"\bbeats?\b"), "supported", "weakened"),
    (re.compile(r"\bimprov\w*"), "supported", "weakened"),
    (re.compile(r"\bpositive result\w*"), "supported", "weakened"),
    (re.compile(r"\b(?:target|criterion|criteria|threshold) met\b"), "supported", "weakened"),
    # Plain negatives ("beaten by the baseline" is the passive of beat).
    (re.compile(r"\bunsupported\b"), "weakened", None),
    (re.compile(r"\bnegative result\w*"), "weakened", None),
    (re.compile(r"\bweaken\w*"), "weakened", None),
    (re.compile(r"\binconclusive\b"), "weakened", None),
    (re.compile(r"\bmixed (?:results?|evidence|findings|signals?)\b"), "weakened", None),
    (re.compile(r"\bpartial(?:ly)? support\w*"), "weakened", None),
    (re.compile(r"\bno (?:significant )?effect\b"), "weakened", None),
    (re.compile(r"\bnot significant\b|\binsignificant\b"), "weakened", None),
    (re.compile(r"\bbelow (?:the )?baseline\b"), "weakened", None),
    (re.compile(r"\bbeaten\b"), "weakened", None),
    (re.compile(r"\bworse than\b"), "weakened", None),
    (re.compile(r"\bunderperform\w*"), "weakened", None),
)

_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|neither|nor|without|cannot|can't|couldn't|didn't|"
    r"doesn't|wasn't|weren't|fail(?:ed|s)?(?:\s+to)?|unable\s+to|far\s+from)\b"
)
# Negation scope ends at a clause boundary.
_CLAUSE_BOUNDARIES = (". ", "; ", ", ", " but ", " however ", " although ", " yet ")


def _negated_in_clause(text: str, match_start: int) -> bool:
    window = text[max(0, match_start - 40):match_start]
    for boundary in _CLAUSE_BOUNDARIES:
        idx = window.rfind(boundary)
        if idx >= 0:
            window = window[idx + len(boundary):]
    return bool(_NEGATION_RE.search(window))


class ExperimentService:
    def __init__(
        self,
        *,
        store: BaseStateStore,
        blobs: BlobStore | None = None,
    ) -> None:
        self.store = store
        # Gate lints read submitted (pinned) bytes from here, never the
        # working tree. Optional only for direct construction in tests; the
        # composition root always injects it.
        self.blobs = blobs

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
        name = validate_experiment_name(name)
        if not intent.strip():
            raise ValidationError("intent is required")
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            self._reject_stopped_project(conn=conn, project_id=project_id)
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
                "and graph all live there. Stage anything the sandbox run needs "
                "inside it before sandbox.request, then explicitly copy out or "
                "upload results you want to keep."
            )
            return state

    def _reject_stopped_project(self, *, conn, project_id: str) -> None:
        row = conn.execute("SELECT status FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is not None and row["status"] == "stopped":
            raise ValidationError("project is stopped; new experiments are not allowed")

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
        if debt < REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD:
            return

        open_wave = conn.execute(
            """
            SELECT id, status FROM syntheses
            WHERE project_id = ? AND status NOT IN ('published', 'abandoned')
            ORDER BY created_seq DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        threshold = REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD
        if open_wave is not None:
            raise WorkflowError(
                "project reflection is required before creating another experiment: "
                f"{debt} experiments have finished since the last published "
                f"reflection (threshold {threshold}), and reflection wave "
                f"{open_wave['id']} is {open_wave['status']!r}. Finish and publish "
                "that reflection wave; its approved change spec will create the "
                "next experiment wave."
            )

        if published_id:
            since = "since the last published reflection"
        else:
            since = "and no project reflection has been published yet"
        raise WorkflowError(
            "project reflection is required before creating another experiment: "
            f"{debt} experiments have finished {since} (threshold {threshold}). "
            "Start a reflection wave with reflection.create and publish it before "
            "creating another experiment."
        )

    def create_from_synthesis(
        self,
        *,
        conn,
        project_id: str,
        synthesis_id: str,
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
                "source_synthesis_id": synthesis_id,
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
            SELECT id, corpus_json FROM syntheses
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
        # `intent` is the durable one-line headline (the experiment's title in
        # the UI). The full design — hypothesis, method, evaluation, risks — now
        # lives in the plan.md resource, which is the single source of truth and
        # the face the reviewer evaluates. We no longer fold the structured
        # fields into intent. For back-compat, if a caller supplied only those
        # fields, fall back to the first non-empty one so intent is never blank.
        if intent.strip():
            return intent.strip()
        for value in (title, hypothesis, design, success_criteria, risks):
            if value and value.strip():
                return value.strip()
        return ""

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
            data["storage_objects"] = self._storage_objects_for_experiment(
                conn=conn,
                project_id=str(data["project_id"]),
                experiment_id=experiment_id,
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
        suggested_status = self._infer_claim_status_from_conclusion(conclusion)
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

    def _infer_claim_status_from_conclusion(self, conclusion: str) -> str | None:
        """Conservative status hint from a free-text conclusion.

        Word-bounded markers vote for a status; a negation cue in the same
        clause flips the marker to its negated vote, and any ambiguity — an
        unclear negated marker (e.g. "does not contradict") or votes for more
        than one status — returns None rather than guessing. A wrong
        suggestion here can flip a canonical claim the wrong way, so silence
        beats cleverness.
        """
        text = " ".join(conclusion.lower().split())
        votes: set[str] = set()
        for pattern, plain_vote, negated_vote in _CLAIM_STATUS_MARKERS:
            for match in pattern.finditer(text):
                vote = (
                    negated_vote
                    if _negated_in_clause(text, match.start())
                    else plain_vote
                )
                if vote is None:
                    return None
                votes.add(vote)
        if len(votes) != 1:
            return None
        return votes.pop()

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
            passed = any(
                row.get("role") == review.role
                and row.get("verdict") == "pass"
                and row.get("target_snapshot_id") == snapshot_id
                for row in experiment.get("reviews", [])
            )
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

    def _storage_objects_for_experiment(
        self, *, conn, project_id: str, experiment_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT id, name, version, kind, content_sha256, size_bytes,
                   content_type, status, expires_at, producing_run, source_uri,
                   notes, created_at, updated_at, last_accessed_at
            FROM storage_objects
            WHERE project_id = ? AND producing_experiment_id = ?
              AND status != 'deleted'
            ORDER BY kind, name, version DESC, created_seq DESC
            """,
            (project_id, experiment_id),
        ).fetchall()
        return rows_to_dicts(rows=rows)

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
        if transition == "retry_running":
            if status == "running":
                return "running"
            options = ", ".join(t["transition"] for t in allowed_transitions_for(status))
            raise WorkflowError(
                f"transition {transition!r} is not allowed from {status!r}; "
                f"allowed from here: {options}"
            )
        forward = GATE_TABLE.get(status)
        if forward is None or forward.name != transition:
            options = ", ".join(t["transition"] for t in allowed_transitions_for(status))
            raise WorkflowError(
                f"transition {transition!r} is not allowed from {status!r}; "
                f"allowed from here: {options}"
            )
        for requirement in forward.requirements:
            if not self._has_resource_role(
                conn=conn,
                experiment_id=experiment_id,
                role=requirement.role,
            ):
                raise WorkflowError(requirement.error)
            self._run_validator(
                conn=conn, experiment_id=experiment_id, name=requirement.validator
            )
        if forward.review is not None and not self._has_passing_review(
            conn=conn,
            experiment_id=experiment_id,
            role=forward.review.role,
        ):
            raise WorkflowError(forward.review.error)
        return forward.to_status

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

        Gates lint the bytes pinned at resource.associate — never the working
        tree — so fixing an artifact means fix the file AND re-associate it.
        """
        if self.blobs is None:
            raise WorkflowError(
                f"{what}: no blob store is configured; gated artifacts cannot be linted"
            )
        attempt = conn.execute(
            "SELECT attempt_index FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if attempt is None:
            raise NotFoundError(f"experiment not found: {experiment_id}")
        return pinned_artifact_text(
            conn=conn,
            blobs=self.blobs,
            target_type="experiment",
            target_id=experiment_id,
            role=role,
            attempt_index=int(attempt["attempt_index"]),
            what=what,
        )

    def _validate_plan_sections(self, *, conn, experiment_id: str) -> None:
        """Block submit_design unless the current attempt's SUBMITTED plan fills
        in the required spine. Lints the bytes pinned at associate; editing the
        live file changes nothing until it is re-associated."""
        plan_text, _, _ = self._pinned_text(
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

    def _validate_results_report(self, *, conn, experiment_id: str) -> None:
        """Block submit_results unless the current attempt's SUBMITTED report
        passes the report lint — including every relative figure link having
        submitted figure content (captured when the report was associated)."""
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

        problems = report_problems(report_text, figure_problem=figure_problem)
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

    def target_snapshot_id(self, *, conn, experiment_id: str) -> str:
        return self._target_snapshot_id(conn=conn, experiment_id=experiment_id)

    def _target_snapshot_id(self, *, conn, experiment_id: str) -> str:
        experiment = self.get_state(experiment_id=experiment_id, conn=conn)
        return review_snapshot_id(target_type="experiment", target=experiment)
