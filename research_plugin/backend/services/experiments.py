"""Experiment state service."""

from __future__ import annotations

import json
import re
from typing import Any

from ..execution.sync_dirs import local_experiment_sync_dir
from ..utils import NotFoundError, ValidationError, WorkflowError
from ..utils import new_id
from ..state.store import StateStore, row_to_dict, rows_to_dicts
from ..utils import now_iso


TERMINAL_STATUSES = frozenset({"complete", "failed", "abandoned"})

# (from_status, transition) -> next_status. Single source of truth for the
# forward workflow graph, shared by _next_status (enforcement) and
# allowed_transitions_for (discovery surfaced on get_state + in errors).
# `abandon`/`mark_failed` are handled separately (available from any
# non-terminal status), so they are not listed here.
TRANSITION_GRAPH: dict[tuple[str, str], str] = {
    ("planned", "submit_design"): "design_review",
    ("design_review", "mark_ready_to_run"): "ready_to_run",
    ("ready_to_run", "start_running"): "running",
    ("running", "submit_results"): "experiment_review",
    ("experiment_review", "complete"): "complete",
}
# Plain-language precondition for transitions gated on more than just status, so
# the agent learns the requirement up front instead of via a sequence of errors.
TRANSITION_REQUIREMENTS: dict[str, str] = {
    "submit_design": (
        "a 'plan' resource must be synced & associated to this experiment, with "
        "the required plan section headers present"
    ),
    "mark_ready_to_run": "a passing design_reviewer review",
    "submit_results": "a 'result' resource must be synced & associated to this experiment",
    "complete": "a passing experiment_reviewer review",
}


def allowed_transitions_for(status: str) -> list[dict[str, Any]]:
    """Transitions available from ``status``, with precondition hints.

    Surfaced on ``experiment.get_state`` and in 'not allowed' errors so the
    agent can see what to do next (and what each step requires) without
    trial-and-error.
    """
    if status in TERMINAL_STATUSES:
        return []
    out: list[dict[str, Any]] = []
    for (frm, transition), nxt in TRANSITION_GRAPH.items():
        if frm == status:
            entry: dict[str, Any] = {"transition": transition, "leads_to": nxt}
            if transition in TRANSITION_REQUIREMENTS:
                entry["requires"] = TRANSITION_REQUIREMENTS[transition]
            out.append(entry)
    out.append({"transition": "abandon", "leads_to": "abandoned"})
    out.append({"transition": "mark_failed", "leads_to": "failed"})
    return out

# --- Plan schema (PRD-style) -------------------------------------------------
# plan.md is the face of the experiment in the UI and the artifact the design
# reviewer evaluates. We enforce a small REQUIRED spine — the minimum that makes
# a plan readable (Summary), motivated (Objective & hypothesis), and judgeable
# (Evaluation) — and leave Method/Outputs/Risks to the design reviewer's
# judgment. See skills/research-workflow/plan-template.md.
#
# Each entry is (canonical_name, match_key): a plan heading satisfies the
# section when its normalized text starts with match_key. The lint is
# deliberately dumb (heading present + non-empty body); whether the content is
# *sufficient* is the design reviewer's call, not the linter's.
REQUIRED_PLAN_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Summary", "summary"),
    ("Objective & hypothesis", "objective"),
    ("Evaluation", "evaluation"),
)

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _normalize_heading(text: str) -> str:
    """Lowercase, expand '&' to 'and', collapse to space-separated words."""
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def plan_sections_missing(plan_text: str) -> list[str]:
    """Return the canonical names of REQUIRED plan sections that are absent or
    empty. A section counts as present when its heading exists and the body
    beneath it — up to the next same-or-higher-level heading — contains
    non-whitespace text. HTML comments are stripped first, so they neither count
    as content nor register as headings; template guidance therefore lives in
    comments precisely so an unfilled section reads as empty here."""
    text = _HTML_COMMENT_RE.sub("", plan_text)
    headings = [
        (m.start(), len(m.group(1)), _normalize_heading(m.group(2)), m.end())
        for m in _HEADING_RE.finditer(text)
    ]
    missing: list[str] = []
    for canonical, key in REQUIRED_PLAN_SECTIONS:
        idx = next((i for i, h in enumerate(headings) if h[2].startswith(key)), None)
        if idx is None:
            missing.append(canonical)
            continue
        level, body_start = headings[idx][1], headings[idx][3]
        body_end = len(text)
        for nxt_start, nxt_level, _, _ in headings[idx + 1:]:
            if nxt_level <= level:
                body_end = nxt_start
                break
        if not text[body_start:body_end].strip():
            missing.append(canonical)
    return missing

# Agent-facing projection of get_state. get_state is the "give me the detail"
# call, so unlike status_and_next we KEEP the substance (review findings/notes,
# intent, conclusion, the resource list). We only drop the pure waste: the
# duplicate all-attempts `resources` list (a byte-for-byte copy of
# current_attempt_resources for single-attempt experiments), per-resource
# derived/bookkeeping fields (version_token — itself path:mtime:mtime:size —,
# mtime_ns, the two usually-equal *_version_id, the three timestamps, repeated
# project_id, constant created_by/git_commit/association_attempt_index), and
# review internals (target_snapshot_id, request_id/session_id/target_*/
# project_id). The UI keeps the full shape (it calls the service method
# directly). See docs/MCP_SERVER_CONTRACT.md.
_SLIM_RESOURCE_FIELDS = ("id", "association_role", "path", "kind", "size_bytes", "missing", "title")
_PRIOR_RESOURCE_FIELDS = ("id", "association_role", "path", "association_attempt_index")
_SLIM_CLAIM_FIELDS = ("id", "statement", "confidence", "status", "scope")
_SLIM_REVIEW_FIELDS = ("id", "role", "verdict", "created_at", "findings", "notes", "evidence")


def slim_experiment_state(full: dict[str, Any]) -> dict[str, Any]:
    """Project a full get_state down to the agent-facing shape (detail, no waste)."""
    attempt = full.get("attempt_index")
    all_resources = full.get("resources", [])
    current = full.get("current_attempt_resources")
    if current is None:
        current = [r for r in all_resources if r.get("association_attempt_index") == attempt]
    prior = [r for r in all_resources if r.get("association_attempt_index") != attempt]

    slim: dict[str, Any] = {
        "id": full.get("id"),
        "status": full.get("status"),
        "attempt_index": attempt,
        "intent": full.get("intent"),
        "conclusion": full.get("conclusion"),
        "revision_context": full.get("revision_context"),
        "created_at": full.get("created_at"),
        "updated_at": full.get("updated_at"),
        "allowed_transitions": full.get(
            "allowed_transitions", allowed_transitions_for(str(full.get("status", "")))
        ),
        "tested_claims": [
            {field: claim.get(field) for field in _SLIM_CLAIM_FIELDS}
            for claim in full.get("tested_claims", [])
        ],
        "current_attempt_resources": [
            {field: res.get(field) for field in _SLIM_RESOURCE_FIELDS}
            for res in current
        ],
        "reviews": [
            {field: review.get(field) for field in _SLIM_REVIEW_FIELDS}
            for review in full.get("reviews", [])
        ],
    }
    # Only surface prior-attempt artifacts (as compact references) when a rerun
    # actually produced them — keeps single-attempt experiments lean.
    if prior:
        slim["prior_attempt_resources"] = [
            {field: res.get(field) for field in _PRIOR_RESOURCE_FIELDS}
            for res in prior
        ]
    return slim


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
            local_experiment_sync_dir(
                repo_root=self.store.repo_root,
                experiment_id=experiment_id,
            ).mkdir(parents=True, exist_ok=True)
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
            data["allowed_transitions"] = allowed_transitions_for(str(data.get("status", "")))
            return data
        finally:
            if owns_conn:
                conn.close()

    def get_state_agent(self, *, experiment_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Agent/MCP-facing get_state: full detail, minus the pure waste."""
        return slim_experiment_state(
            self.get_state(experiment_id=experiment_id, project_id=project_id)
        )

    def list_experiments_agent(self, *, project_id: str | None = None) -> dict[str, Any]:
        """Agent/MCP-facing experiment.list: each experiment slimmed like get_state."""
        full = self.list_experiments(project_id=project_id)
        return {"experiments": [slim_experiment_state(exp) for exp in full["experiments"]]}

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
        next_status = TRANSITION_GRAPH.get((status, transition))
        if next_status is None:
            options = ", ".join(t["transition"] for t in allowed_transitions_for(status))
            raise WorkflowError(
                f"transition {transition!r} is not allowed from {status!r}; "
                f"allowed from here: {options}"
            )
        if transition == "submit_design":
            if not self._has_resource_role(
                conn=conn,
                experiment_id=experiment_id,
                role="plan",
            ):
                raise WorkflowError("an experiment plan resource must be synced before design review")
            self._validate_plan_sections(conn=conn, experiment_id=experiment_id)
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

    def _validate_plan_sections(self, *, conn, experiment_id: str) -> None:
        """Block submit_design unless the current-attempt plan file fills in the
        required spine. Reads the live file (the same content the UI shows and
        the reviewer reads), not a cached snapshot."""
        row = conn.execute(
            """
            SELECT r.path
            FROM resource_associations a
            JOIN resources r ON r.id = a.resource_id
            WHERE a.target_type = 'experiment' AND a.target_id = ? AND a.role = 'plan'
              AND a.attempt_index = (SELECT attempt_index FROM experiments WHERE id = ?)
              AND r.deleted = 0
            ORDER BY a.rowid DESC
            LIMIT 1
            """,
            (experiment_id, experiment_id),
        ).fetchone()
        if row is None:
            # _has_resource_role already guaranteed a plan exists; defensive only.
            raise WorkflowError("an experiment plan resource must be synced before design review")
        plan_path = self.store.repo_root / row["path"]
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WorkflowError(
                f"plan resource {row['path']!r} could not be read for design review: {exc}"
            ) from exc
        missing = plan_sections_missing(plan_text)
        if missing:
            raise WorkflowError(
                "experiment plan is missing required sections before design review: "
                + ", ".join(missing)
                + ". Fill in the plan template's required spine — Summary; "
                "Objective & hypothesis; Evaluation — see "
                "skills/research-workflow/plan-template.md."
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
