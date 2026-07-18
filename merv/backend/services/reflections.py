"""Project reflection wave state service.

A reflection wave is the project-level counterpart of an experiment: a gated
record whose artifacts are the living project logic graph (role
'project_graph'), a concise reflection document (role 'reflection_doc'), and
the reviewed change spec (role 'change_spec'), produced by reconciling a
roster of differentiated per-lens reflections (role 'reflection_lens_doc').
Gates check envelopes only; the story's honesty and the belief-state update
are the reflection reviewer's call, and what the graph says is the agent's
design.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.experiment_names import validate_experiment_name
from ..domain.experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    active_experiment_cap_would_exceed_message,
)
from ..domain.gates import RequirementState, ReviewState, decide_gated_transition
from ..domain.graph_lint import graph_problems
from ..domain.reflection_artifacts import (
    claim_refs,
    current_reflection_requirement_resource,
    graph_diff,
    graph_diff_summary,
    parse_change_spec,
    reflection_coverage_for,
    reflection_doc_review_problems,
    reflection_gate_resource_label,
    reflection_gate_review_label,
    reflection_lens_checklist_items,
    reflection_requirement_roles,
    validate_reflection_roster,
)
from ..domain.reflection_policy import (
    covered_terminal_ids,
    post_publish_guidance,
    reflection_signal_state,
)
from ..artifacts.resource_selection import preferred_associated_resource
from ..domain.review_snapshot import review_snapshot_id
from ..domain.reflection_gates import (
    REFLECTION_GATE_TABLE,
    REFLECTION_TERMINAL_STATUSES,
    allowed_reflection_transitions_for,
)
from ..artifacts.roles import PROJECT_GRAPH_ROLES
from ..domain.vocabulary import EXPERIMENT_TERMINAL_STATUSES
from ..ports.reflection_writers import (
    ReflectionClaimWriter,
    ReflectionExperimentWriter,
)
from ..artifacts.pinned import PinnedStore, resubmit_hint
from ..state.store import BaseStateStore, next_created_seq, row_to_dict, rows_to_dicts
from ..utils import NotFoundError, WorkflowError, new_id, now_iso
from .review_gate import review_gate_state


class ReflectionService:
    def __init__(
        self,
        *,
        store: BaseStateStore,
        claims: ReflectionClaimWriter,
        experiment_writer: ReflectionExperimentWriter,
        pinned: PinnedStore | None = None,
    ) -> None:
        self.store = store
        self.claims = claims
        self.experiment_writer = experiment_writer
        # Gate lints read submitted (pinned) bytes from here, never the
        # working tree (see artifacts/pinned.py).
        self.pinned = pinned

    # ---- create ----

    def create(
        self,
        *,
        title: str = "",
        lenses: list[dict[str, Any]] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        roster = validate_reflection_roster(lenses=lenses or [])
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            open_row = conn.execute(
                """
                SELECT id, status FROM reflections
                WHERE project_id = ? AND status NOT IN ('published', 'abandoned')
                ORDER BY created_seq DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if open_row is not None:
                raise WorkflowError(
                    f"a reflection wave is already open: {open_row['id']} is "
                    f"{open_row['status']!r}. Finish or abandon it before "
                    "starting a new one — the project graph is one living "
                    "artifact and only one wave may edit it at a time"
                )
            reflection_id = new_id(prefix="syn")
            now = now_iso()
            corpus = self._corpus_snapshot(conn=conn, project_id=project_id)
            conn.execute(
                """
                INSERT INTO reflections
                  (id, project_id, title, status, attempt_index, revision_context,
                   roster_json, corpus_json, created_at, updated_at, created_seq)
                VALUES (?, ?, ?, 'reflecting', 1, '', ?, ?, ?, ?, ?)
                """,
                (
                    reflection_id,
                    project_id,
                    title.strip(),
                    json.dumps(roster, sort_keys=True),
                    json.dumps(corpus, sort_keys=True),
                    now,
                    now,
                    next_created_seq(conn=conn, table="reflections"),
                ),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="reflection.created",
                target_type="reflection",
                target_id=reflection_id,
                payload={
                    "title": title.strip(),
                    "lenses": [lens["id"] for lens in roster],
                    "corpus_terminal_experiments": len(corpus["terminal_experiments"]),
                },
            )
            return self.get_state(reflection_id=reflection_id, conn=conn)

    def _corpus_snapshot(self, *, conn, project_id: str) -> dict[str, Any]:
        terminal = ", ".join(f"'{s}'" for s in sorted(EXPERIMENT_TERMINAL_STATUSES))
        exp_rows = conn.execute(
            f"""
            SELECT id, name, attempt_index, status FROM experiments
            WHERE project_id = ? AND status IN ({terminal})
            ORDER BY created_at, id
            """,
            (project_id,),
        ).fetchall()
        claim_rows = conn.execute(
            "SELECT id, status FROM claims WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        experiments = rows_to_dicts(rows=exp_rows)
        previous = self.latest_published(conn=conn, project_id=project_id)
        covered = covered_terminal_ids(
            None if previous is None else (previous.get("corpus") or {})
        )
        # The wave's new signal: terminal experiments the last published wave
        # never saw. The reflection still reads the whole project; these name
        # why it is happening now. Previous lens-reflection paths let a lens
        # learn from its own prior round without the orchestrator digging.
        return {
            "captured_at": now_iso(),
            "terminal_experiments": experiments,
            "claims": rows_to_dicts(rows=claim_rows),
            "new_terminal_experiments": [
                {"id": exp["id"], "name": exp["name"], "status": exp["status"]}
                for exp in experiments
                if str(exp["id"]) not in covered
            ],
            "previous_published_reflection_id": (
                None if previous is None else previous["id"]
            ),
            "previous_lens_reflections": (
                {}
                if previous is None
                else {
                    str(lens["lens_id"]): lens["path"]
                    for lens in previous["reflection_coverage"]["lenses"]
                    if lens.get("covered")
                }
            ),
        }

    # ---- read ----

    def get_state(
        self, *, reflection_id: str, project_id: str | None = None, conn=None
    ) -> dict[str, Any]:
        owns_conn = conn is None
        if conn is None:
            conn = self.store.connect()
        try:
            if owns_conn:
                project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                "SELECT * FROM reflections WHERE id = ?", (reflection_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"reflection not found: {reflection_id}")
            data = row_to_dict(row=row) or {}
            if project_id is not None and data["project_id"] != project_id:
                raise NotFoundError(
                    f"reflection not found in project {project_id}: {reflection_id}"
                )
            data["roster"] = json.loads(str(data.pop("roster_json", "[]")))
            data["corpus"] = json.loads(str(data.pop("corpus_json", "{}")))
            resource_rows = conn.execute(
                """
                SELECT r.*, a.role AS association_role, a.attempt_index AS association_attempt_index,
                       a.version_id AS association_version_id, a.created_seq AS association_rowid
                FROM resources r
                JOIN resource_associations a ON a.resource_id = r.id
                WHERE a.target_type = 'reflection' AND a.target_id = ?
                ORDER BY a.attempt_index, a.role, r.path
                """,
                (reflection_id,),
            ).fetchall()
            data["resources"] = rows_to_dicts(rows=resource_rows)
            data["current_attempt_resources"] = [
                res
                for res in data["resources"]
                if res.get("association_attempt_index") == data["attempt_index"]
            ]
            claim_rows = conn.execute(
                """
                SELECT sc.reflection_id, sc.claim_id, sc.op, sc.claim_key,
                       sc.created_at, c.statement, c.status, c.confidence
                FROM reflection_claim_changes sc
                JOIN claims c ON c.id = sc.claim_id
                WHERE sc.reflection_id = ?
                ORDER BY sc.created_at, sc.claim_id
                """,
                (reflection_id,),
            ).fetchall()
            data["materialized_claims"] = rows_to_dicts(rows=claim_rows)
            experiment_rows = conn.execute(
                """
                SELECT se.reflection_id, se.experiment_id, se.proposal_key,
                       se.created_at, e.name, e.intent, e.status
                FROM reflection_experiments se
                JOIN experiments e ON e.id = se.experiment_id
                WHERE se.reflection_id = ?
                ORDER BY se.created_at, se.experiment_id
                """,
                (reflection_id,),
            ).fetchall()
            data["materialized_experiments"] = rows_to_dicts(rows=experiment_rows)
            if data.get("status") == "published" and data["materialized_experiments"]:
                data["post_publish_guidance"] = post_publish_guidance(
                    materialized_experiments=data["materialized_experiments"],
                )
            review_rows = conn.execute(
                """
                SELECT * FROM reviews
                WHERE target_type = 'reflection' AND target_id = ?
                ORDER BY created_seq DESC
                """,
                (reflection_id,),
            ).fetchall()
            reviews = rows_to_dicts(rows=review_rows)
            for review in reviews:
                review["findings"] = json.loads(review.pop("findings_json", "[]"))
                review["evidence"] = json.loads(review.pop("evidence_json", "{}"))
            data["reviews"] = reviews
            data["reflection_coverage"] = self._reflection_coverage(reflection=data)
            data["project_graph_diff"] = self._project_graph_diff(
                conn=conn, reflection=data
            )
            data["gate_checklist"] = self._gate_checklist(conn=conn, reflection=data)
            data["allowed_transitions"] = allowed_reflection_transitions_for(
                str(data.get("status", ""))
            )
            return data
        finally:
            if owns_conn:
                conn.close()


    def list_reflections(self, *, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT id FROM reflections WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return {
                "reflections": [
                    self.get_state(reflection_id=row["id"], conn=conn) for row in rows
                ]
            }
        finally:
            conn.close()

    def overview(self, *, project_id: str | None = None) -> dict[str, Any]:
        """All waves plus the current reflection signal for project UI views."""
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT id FROM reflections WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            reflections = [
                self.get_state(reflection_id=row["id"], conn=conn) for row in rows
            ]
            signal = self.reflection_signal(project_id=project_id, conn=conn)
            open_wave = self.open_reflection(conn=conn, project_id=project_id)
            published = self.latest_published(conn=conn, project_id=project_id)
            return {
                "reflections": reflections,
                "current": open_wave or published,
                "open_reflection": open_wave,
                "latest_published": published,
                "signal": signal,
            }
        finally:
            conn.close()

    def project_logic_graph_selection(self, *, project_id: str) -> dict[str, Any]:
        """Select the current project graph wave and reflection signal.

        The UI prefers the open wave's graph while the wave is open,
        falling back to the latest published graph when the open wave has not
        submitted one yet. The transport layer owns response shaping; this
        service owns the record reads and selection policy.
        """
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            signal = self.reflection_signal(project_id=project_id, conn=conn)
            reflection = self.open_reflection(conn=conn, project_id=project_id)
            graph_resource = self._project_graph_resource(reflection=reflection)
            if reflection is None or graph_resource is None:
                published = self.latest_published(conn=conn, project_id=project_id)
                published_graph = self._project_graph_resource(reflection=published)
                if published is not None and published_graph is not None:
                    reflection = published
                    graph_resource = published_graph
            return {
                "signal": signal,
                "reflection": reflection,
                "graph_resource": graph_resource,
            }
        finally:
            conn.close()

    def open_reflection(self, *, conn, project_id: str) -> dict[str, Any] | None:
        """The one non-terminal wave for the project, fully hydrated, or None."""
        row = conn.execute(
            """
            SELECT id FROM reflections
            WHERE project_id = ? AND status NOT IN ('published', 'abandoned')
            ORDER BY created_seq DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_state(reflection_id=row["id"], conn=conn)

    def latest_published(self, *, conn, project_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT id FROM reflections
            WHERE project_id = ? AND status = 'published'
            ORDER BY published_at DESC, created_seq DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_state(reflection_id=row["id"], conn=conn)

    @staticmethod
    def _project_graph_resource(
        *, reflection: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if reflection is None:
            return None
        return preferred_associated_resource(
            resources=reflection.get("resources", []),
            attempt=reflection.get("attempt_index"),
            roles=PROJECT_GRAPH_ROLES,
        )

    def _project_graph_diff(self, *, conn, reflection: dict[str, Any]) -> dict[str, Any]:
        current_resource = self._project_graph_resource(reflection=reflection)
        current_version_id = str(
            (
                reflection.get("published_graph_version_id")
                if reflection.get("status") == "published"
                else None
            )
            or (current_resource or {}).get("association_version_id")
            or ""
        )
        base = self._previous_published_graph_ref(conn=conn, reflection=reflection)
        result: dict[str, Any] = {
            "available": False,
            "reason": "",
            "summary": "",
            "base_reflection_id": base.get("reflection_id") if base else None,
            "base_graph_version_id": base.get("graph_version_id") if base else None,
            "current_reflection_id": reflection.get("id"),
            "current_graph_version_id": current_version_id or None,
            "problems": [],
        }
        if not current_version_id:
            result.update(
                {
                    "reason": "no_current_project_graph",
                    "summary": "No current project graph is associated for this reflection wave.",
                }
            )
            return result
        if base is None or not base.get("graph_version_id"):
            result.update(
                {
                    "reason": "no_previous_project_graph",
                    "summary": "No previous published project graph is available to compare.",
                }
            )
            return result

        base_graph, base_problems = self._load_graph_for_diff(
            conn=conn,
            version_id=str(base["graph_version_id"]),
            role="project_graph",
            what="previous project logic graph",
        )
        current_graph, current_problems = self._load_graph_for_diff(
            conn=conn,
            version_id=current_version_id,
            role=str((current_resource or {}).get("association_role") or "project_graph"),
            what="current project logic graph",
        )
        problems = [*base_problems, *current_problems]
        if problems or base_graph is None or current_graph is None:
            result.update(
                {
                    "reason": "graph_unavailable",
                    "summary": "Project graph diff is unavailable because one graph cannot be read.",
                    "problems": problems,
                }
            )
            return result

        diff = graph_diff(base_graph=base_graph, current_graph=current_graph)
        result.update(diff)
        result["available"] = True
        result["reason"] = ""
        result["summary"] = graph_diff_summary(diff=diff)
        return result

    def _previous_published_graph_ref(
        self, *, conn, reflection: dict[str, Any]
    ) -> dict[str, Any] | None:
        project_id = str(reflection.get("project_id") or "")
        status = str(reflection.get("status") or "")
        current_id = str(reflection.get("id") or "")
        params: tuple[Any, ...]
        if status == "published":
            query = """
                SELECT id, published_graph_version_id
                FROM reflections
                WHERE project_id = ? AND status = 'published'
                  AND id != ? AND created_seq < ?
                ORDER BY published_at DESC, created_seq DESC
                LIMIT 1
                """
            params = (project_id, current_id, int(reflection.get("created_seq") or 0))
        else:
            query = """
                SELECT id, published_graph_version_id
                FROM reflections
                WHERE project_id = ? AND status = 'published'
                ORDER BY published_at DESC, created_seq DESC
                LIMIT 1
                """
            params = (project_id,)
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        return {
            "reflection_id": row["id"],
            "graph_version_id": row["published_graph_version_id"],
        }

    def _load_graph_for_diff(
        self, *, conn, version_id: str, role: str, what: str
    ) -> tuple[dict[str, Any] | None, list[str]]:
        if self.pinned is None:
            return None, [f"{what}: no blob store is configured"]
        try:
            text = self.pinned.text_for_version(
                conn=conn,
                version_id=version_id,
                what=what,
                role=role,
            )
        except WorkflowError as exc:
            return None, [str(exc)]
        problems = graph_problems(text)
        if problems:
            return None, [f"{what}: {problem}" for problem in problems]
        data = json.loads(text)
        return data, []

    def _reflection_coverage(self, *, reflection: dict[str, Any]) -> dict[str, Any]:
        """Which roster lenses have a current-attempt reflection associated.

        A reflection covers lens L when its file is named ``<L>.md`` (any
        directory) — the dumb, predictable convention each fan-out subagent is
        told to follow.
        """
        return reflection_coverage_for(reflection=reflection)

    def _gate_checklist(self, *, conn, reflection: dict[str, Any]) -> dict[str, Any]:
        """Current reflection-wave gate as machine-readable checklist data.

        This is the reflection counterpart of experiment state gate_checklist:
        it derives from the declarative reflection gate table, reports exactly
        which lens/artifact/review items are missing or invalid, and uses the
        same pinned-byte validators that transitions use.
        """
        status = str(reflection.get("status") or "")
        forward = REFLECTION_GATE_TABLE.get(status)
        if forward is None:
            return {
                "status": status,
                "transition": None,
                "leads_to": None,
                "ready": status in REFLECTION_TERMINAL_STATUSES,
                "items": [],
            }

        if status == "reflecting":
            items = reflection_lens_checklist_items(reflection=reflection)
        else:
            items = []
            for requirement in forward.requirements:
                resource = current_reflection_requirement_resource(
                    reflection=reflection, role=requirement.role
                )
                present = resource is not None
                problems: list[str] = []
                state = "present" if present else "missing"
                if present and requirement.validator:
                    try:
                        self._run_validator(
                            conn=conn, reflection=reflection, name=requirement.validator
                        )
                    except WorkflowError as exc:
                        problems = [str(exc)]
                    state = "invalid" if problems else "valid"
                item: dict[str, Any] = {
                    "id": f"resource:{requirement.role}",
                    "kind": "resource",
                    "role": requirement.role,
                    "label": reflection_gate_resource_label(role=requirement.role),
                    "satisfied": present and not problems,
                    "status": state,
                    "gate": requirement.gate,
                    "action": requirement.action,
                }
                if requirement.validator:
                    item["validator"] = requirement.validator
                if resource is not None:
                    item["path"] = resource.get("path")
                    item["version_id"] = resource.get("association_version_id")
                    item["association_role"] = resource.get("association_role")
                if not present:
                    item["missing"] = (
                        requirement.missing or f"{requirement.role} resource"
                    )
                if problems:
                    item["problems"] = problems
                items.append(item)

        if forward.review is not None:
            review = forward.review
            snapshot_id = review_snapshot_id(target_type="reflection", target=reflection)
            gate_state = review_gate_state(
                conn=conn,
                project_id=str(reflection["project_id"]),
                target_type="reflection",
                target_id=str(reflection["id"]),
                role=review.role,
                snapshot_id=snapshot_id,
            )
            passed = gate_state["satisfied"]
            request = self._latest_review_request(
                conn=conn,
                reflection_id=str(reflection["id"]),
                role=review.role,
                target_snapshot_id=snapshot_id,
            )
            review_status = "passed" if passed else self._review_gate_status(
                request=request
            )
            item = {
                "id": f"review:{review.role}",
                "kind": "review",
                "role": review.role,
                "label": reflection_gate_review_label(role=review.role),
                "satisfied": passed,
                "status": review_status,
                "gate": status,
                "action": (
                    review.pass_action
                    if passed
                    else f"launch_{review.action_name}er"
                ),
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
        reflection_id: str,
        role: str,
        target_snapshot_id: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT id, status, expires_at
            FROM review_requests
            WHERE target_type = 'reflection' AND target_id = ? AND role = ?
              AND target_snapshot_id = ?
            ORDER BY created_seq DESC
            LIMIT 1
            """,
            (reflection_id, role, target_snapshot_id),
        ).fetchone()
        return row_to_dict(row=row)

    def _review_gate_status(self, *, request: dict[str, Any] | None) -> str:
        if request is None:
            return "pending"
        if request.get("status") in {"requested", "started"}:
            return str(request["status"])
        return "pending"

    # ---- transitions ----

    def transition(
        self,
        *,
        reflection_id: str,
        transition: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            reflection = self.get_state(
                reflection_id=reflection_id, project_id=project_id, conn=conn
            )
            status = reflection["status"]
            next_status = self._next_status(
                conn=conn, reflection=reflection, transition=transition
            )
            now = now_iso()
            if transition == "publish":
                self._materialize_change_spec(conn=conn, reflection=reflection)
                conn.execute(
                    """
                    UPDATE reflections
                    SET status = ?, published_at = ?, published_graph_version_id = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_status,
                        now,
                        self._current_graph_version_id(conn=conn, reflection=reflection),
                        now,
                        reflection_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE reflections SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status, now, reflection_id),
                )
            self.store.record_event(
                conn=conn,
                project_id=reflection["project_id"],
                event_type="reflection.transitioned",
                target_type="reflection",
                target_id=reflection_id,
                payload={"from": status, "to": next_status, "transition": transition},
            )
            return self.get_state(reflection_id=reflection_id, conn=conn)

    def _next_status(self, *, conn, reflection: dict[str, Any], transition: str) -> str:
        status = str(reflection["status"])
        forward = REFLECTION_GATE_TABLE.get(status)
        requirement_states: list[RequirementState] = []
        review_state: ReviewState | None = None
        if forward is not None:
            for requirement in forward.requirements:
                validation_error = ""
                present = self._has_resource_role(
                    conn=conn, reflection_id=reflection["id"], role=requirement.role
                )
                if present and requirement.validator:
                    try:
                        self._run_validator(
                            conn=conn, reflection=reflection, name=requirement.validator
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
                    conn=conn, reflection_id=reflection["id"], role=forward.review.role
                )
                review_state = ReviewState(
                    satisfied=bool(gate_state["satisfied"]),
                    error=forward.review.error,
                    blocked_reason=str(gate_state.get("blocked_reason") or ""),
                )
        return decide_gated_transition(
            subject="reflection wave",
            status=status,
            transition=transition,
            terminal_statuses=REFLECTION_TERMINAL_STATUSES,
            direct_transitions={"abandon": "abandoned"},
            forward=forward,
            requirement_states=requirement_states,
            review_state=review_state,
            allowed_transitions=allowed_reflection_transitions_for(status),
        )

    def _has_resource_role(self, *, conn, reflection_id: str, role: str) -> bool:
        roles = reflection_requirement_roles(role=role)
        placeholders = ",".join("?" * len(roles))
        row = conn.execute(
            f"""
            SELECT 1
            FROM resource_associations
            WHERE target_type = 'reflection' AND target_id = ? AND role IN ({placeholders})
              AND attempt_index = (SELECT attempt_index FROM reflections WHERE id = ?)
            LIMIT 1
            """,
            (reflection_id, *roles, reflection_id),
        ).fetchone()
        return row is not None

    def _run_validator(self, *, conn, reflection: dict[str, Any], name: str) -> None:
        if name == "roster":
            self._validate_roster_coverage(conn=conn, reflection=reflection)
        elif name == "graph":
            self._validate_project_graph(conn=conn, reflection=reflection)
        elif name in {"reflection_doc", "synthesis_doc"}:
            self._validate_reflection_doc(conn=conn, reflection=reflection)
        elif name == "change_spec":
            self._validate_change_spec(conn=conn, reflection=reflection)

    def _validate_roster_coverage(self, *, conn, reflection: dict[str, Any]) -> None:
        """The hard 'all lenses before synthesize' requirement: every declared
        lens needs a current-attempt reflection (file named <lens_id>.md) that
        exists and is non-empty on disk. Which insights each reflection holds
        is the synthesizer's and reviewer's business, not the gate's."""
        fresh = self.get_state(reflection_id=reflection["id"], conn=conn)
        coverage = fresh["reflection_coverage"]
        if coverage["missing"]:
            raise WorkflowError(
                "reflections are missing for lens(es): "
                + ", ".join(coverage["missing"])
                + " — each roster lens must have its own reflection associated "
                "(role 'reflection_lens_doc') for the current attempt, in a "
                "file named <lens_id>.md, submitted by its own subagent"
            )
        for lens in coverage["lenses"]:
            text = self._pinned_text(
                conn=conn,
                version_id=lens.get("version_id"),
                path=str(lens["path"]),
                role=str(lens.get("role") or "reflection_lens_doc"),
                what=f"reflection {lens['lens_id']!r}",
            )
            if not text.strip():
                raise WorkflowError(
                    f"reflection for lens {lens['lens_id']!r} ({lens['path']}) is "
                    "empty — write it and re-associate to submit the content"
                )

    def _validate_project_graph(self, *, conn, reflection: dict[str, Any]) -> None:
        row = self._current_role_row_for_roles(
            conn=conn, reflection_id=reflection["id"], roles=PROJECT_GRAPH_ROLES
        )
        if row is None:
            raise WorkflowError(
                "a project logic graph resource must be submitted before reflection review"
            )
        text = self._pinned_text(
            conn=conn,
            version_id=row["version_id"],
            path=str(row["path"]),
            role=str(row["role"]),
            what="project logic graph",
        )
        problems = graph_problems(text)
        if problems:
            raise WorkflowError(
                "project logic graph is not ready for reflection review: "
                + "; ".join(problems)
                + ". Fix the file and re-associate it to submit the revision — "
                "see skills/research-workflow/graph-template.md."
            )

    def _validate_reflection_doc(self, *, conn, reflection: dict[str, Any]) -> None:
        row = self._current_role_row_for_roles(
            conn=conn,
            reflection_id=reflection["id"],
            roles=("reflection_doc", "synthesis_doc"),
        )
        if row is None:
            raise WorkflowError(
                "a reflection document resource must be submitted before reflection review"
            )
        text = self._pinned_text(
            conn=conn,
            version_id=row["version_id"],
            path=str(row["path"]),
            role=str(row["role"]),
            what="reflection document",
        )
        submitted_images = {
            str(image["link_path"])
            for image in conn.execute(
                "SELECT link_path FROM report_figures WHERE report_version_id = ?",
                (row["version_id"],),
            ).fetchall()
        }
        problems = reflection_doc_review_problems(
            text=text,
            submitted_images=submitted_images,
            path=str(row["path"]),
        )
        if problems:
            raise WorkflowError(
                "reflection document is not ready for review: "
                + "; ".join(problems)
                + ". Keep it concise, fix the file, and re-associate it to "
                "submit the revision — see "
                "skills/project-reflection/reflection-artifacts-template.md."
            )

    def _validate_change_spec(self, *, conn, reflection: dict[str, Any]) -> None:
        row = self._current_role_row(
            conn=conn, reflection_id=reflection["id"], role="change_spec"
        )
        if row is None:
            raise WorkflowError(
                "a change spec resource must be submitted before reflection review"
            )
        text = self._pinned_text(
            conn=conn,
            version_id=row["version_id"],
            path=str(row["path"]),
            role="change_spec",
            what="change spec",
        )
        self._parse_change_spec(
            conn=conn,
            project_id=str(reflection["project_id"]),
            text=text,
            path=str(row["path"]),
        )

    def _current_change_spec(self, *, conn, reflection: dict[str, Any]) -> dict[str, Any]:
        row = self._current_role_row(
            conn=conn, reflection_id=reflection["id"], role="change_spec"
        )
        if row is None:
            raise WorkflowError(
                "a change spec resource must be submitted before publish"
            )
        text = self._pinned_text(
            conn=conn,
            version_id=row["version_id"],
            path=str(row["path"]),
            role="change_spec",
            what="change spec",
        )
        return self._parse_change_spec(
            conn=conn,
            project_id=str(reflection["project_id"]),
            text=text,
            path=str(row["path"]),
        )

    def _parse_change_spec(
        self, *, conn, project_id: str, text: str, path: str
    ) -> dict[str, Any]:
        return parse_change_spec(
            text=text,
            path=path,
            claim_exists=lambda claim_id: self._claim_exists(
                conn=conn, project_id=project_id, claim_id=claim_id
            ),
            experiment_name_taken=lambda name: self._experiment_name_exists(
                conn=conn, project_id=project_id, name=name
            ),
            non_terminal_experiments=lambda: self._non_terminal_experiments(
                conn=conn, project_id=project_id
            ),
        )

    def _claim_exists(self, *, conn, project_id: str, claim_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM claims WHERE id = ? AND project_id = ? LIMIT 1",
            (claim_id, project_id),
        ).fetchone()
        return row is not None

    def _experiment_name_exists(self, *, conn, project_id: str, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM experiments WHERE project_id = ? AND lower(name) = lower(?) LIMIT 1",
            (project_id, name),
        ).fetchone()
        return row is not None

    def _non_terminal_experiments(self, *, conn, project_id: str) -> list[str]:
        terminal = ", ".join(f"'{status}'" for status in sorted(EXPERIMENT_TERMINAL_STATUSES))
        rows = conn.execute(
            f"""
            SELECT name, id FROM experiments
            WHERE project_id = ? AND status NOT IN ({terminal})
            ORDER BY created_at, id
            """,
            (project_id,),
        ).fetchall()
        return [str(row["name"] or row["id"]) for row in rows]

    def _materialize_change_spec(self, *, conn, reflection: dict[str, Any]) -> None:
        """Apply the reviewer-approved belief-state update.

        This is called only from the publish transition after the review gate
        passes. Rejected reflections never reach this function, so speculative
        claim edits or experiment specs do not leak into project state.
        """
        project_id = str(reflection["project_id"])
        reflection_id = str(reflection["id"])
        spec = self._current_change_spec(conn=conn, reflection=reflection)
        key_to_claim_id = self._materialize_claim_changes(
            conn=conn,
            project_id=project_id,
            reflection_id=reflection_id,
            changes=spec.get("claim_changes") or [],
        )
        self._materialize_experiment_wave(
            conn=conn,
            project_id=project_id,
            reflection_id=reflection_id,
            key_to_claim_id=key_to_claim_id,
            experiments=spec["decision"].get("experiments") or [],
        )

    def _materialize_claim_changes(
        self,
        *,
        conn,
        project_id: str,
        reflection_id: str,
        changes: list[dict[str, Any]],
    ) -> dict[str, str]:
        key_to_claim_id: dict[str, str] = {}
        for change in changes:
            op = str(change["op"])
            key = str(change.get("key") or "").strip()
            if op == "create":
                claim_id = self.claims.create_from_reflection(
                    conn=conn,
                    project_id=project_id,
                    reflection_id=reflection_id,
                    statement=str(change.get("statement") or ""),
                    scope=str(change.get("scope") or ""),
                    status=str(change.get("status") or "active"),
                    confidence=str(change.get("confidence") or "medium"),
                    rationale=str(change.get("rationale") or ""),
                )
                if key:
                    key_to_claim_id[key] = claim_id
            else:
                claim_id = str(change["claim_id"]).strip()
                self.claims.update_from_reflection(
                    conn=conn,
                    project_id=project_id,
                    reflection_id=reflection_id,
                    claim_id=claim_id,
                    statement=(
                        str(change["statement"]) if "statement" in change else None
                    ),
                    scope=str(change["scope"]) if "scope" in change else None,
                    status=(
                        str(change["status"])
                        if change.get("status") is not None
                        else None
                    ),
                    confidence=(
                        str(change["confidence"])
                        if change.get("confidence") is not None
                        else None
                    ),
                    rationale=str(change.get("rationale") or ""),
                )
            conn.execute(
                """
                INSERT INTO reflection_claim_changes
                  (reflection_id, claim_id, op, claim_key, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (reflection_id, claim_id, op, key, now_iso()),
            )
        return key_to_claim_id

    def _materialize_experiment_wave(
        self,
        *,
        conn,
        project_id: str,
        reflection_id: str,
        key_to_claim_id: dict[str, str],
        experiments: list[dict[str, Any]],
    ) -> None:
        active_count = len(
            self._non_terminal_experiments(conn=conn, project_id=project_id)
        )
        if active_count + len(experiments) > ACTIVE_EXPERIMENT_CAP:
            raise WorkflowError(
                active_experiment_cap_would_exceed_message(
                    active_count=active_count,
                    proposed_count=len(experiments),
                )
            )
        for proposal in experiments:
            name = validate_experiment_name(str(proposal.get("name") or ""))
            intent = str(proposal.get("intent") or "").strip()
            claim_ids = [
                key_to_claim_id.get(ref, ref)
                for ref in claim_refs(proposal)
            ]
            proposal_key = str(proposal.get("key") or "").strip()
            experiment_id = self.experiment_writer.create_from_reflection(
                conn=conn,
                project_id=project_id,
                reflection_id=reflection_id,
                name=name,
                intent=intent,
                claim_ids=claim_ids,
                proposal_key=proposal_key,
                parallelism=str(proposal.get("parallelism") or ""),
            )
            conn.execute(
                """
                INSERT INTO reflection_experiments
                  (reflection_id, experiment_id, proposal_key, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (reflection_id, experiment_id, proposal_key, now_iso()),
            )

    def _current_role_row(self, *, conn, reflection_id: str, role: str):
        return conn.execute(
            """
            SELECT r.path, a.version_id
            FROM resource_associations a
            JOIN resources r ON r.id = a.resource_id
            WHERE a.target_type = 'reflection' AND a.target_id = ? AND a.role = ?
              AND a.attempt_index = (SELECT attempt_index FROM reflections WHERE id = ?)
              AND r.deleted = 0
            ORDER BY a.created_seq DESC
            LIMIT 1
            """,
            (reflection_id, role, reflection_id),
        ).fetchone()

    def _current_role_row_for_roles(
        self, *, conn, reflection_id: str, roles: tuple[str, ...]
    ):
        placeholders = ",".join("?" * len(roles))
        order_cases = " ".join(
            f"WHEN ? THEN {index}" for index, _role in enumerate(roles)
        )
        return conn.execute(
            f"""
            SELECT r.path, a.role, a.version_id
            FROM resource_associations a
            JOIN resources r ON r.id = a.resource_id
            WHERE a.target_type = 'reflection' AND a.target_id = ?
              AND a.role IN ({placeholders})
              AND a.attempt_index = (SELECT attempt_index FROM reflections WHERE id = ?)
              AND r.deleted = 0
            ORDER BY CASE a.role {order_cases} ELSE {len(roles)} END,
                     a.created_seq DESC
            LIMIT 1
            """,
            (reflection_id, *roles, reflection_id, *roles),
        ).fetchone()

    def _current_graph_version_id(self, *, conn, reflection: dict[str, Any]) -> str | None:
        row = self._current_role_row_for_roles(
            conn=conn, reflection_id=reflection["id"], roles=PROJECT_GRAPH_ROLES
        )
        return str(row["version_id"]) if row and row["version_id"] else None

    def _pinned_text(
        self, *, conn, version_id: Any, path: str, role: str, what: str
    ) -> str:
        """The submitted bytes of a pinned association, never the working tree."""
        if self.pinned is None:
            raise WorkflowError(
                f"{what}: no blob store is configured; gated artifacts cannot be linted"
            )
        if not version_id:
            raise WorkflowError(
                f"{what} ({path}) has no pinned version — "
                + resubmit_hint(role=role, path=path)
            )
        return self.pinned.text_for_version(
            conn=conn,
            version_id=str(version_id),
            what=what,
            role=role,
        )

    def _review_gate_state(self, *, conn, reflection_id: str, role: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT project_id FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        return review_gate_state(
            conn=conn,
            project_id=str(row["project_id"]) if row else "",
            target_type="reflection",
            target_id=reflection_id,
            role=role,
            snapshot_id=self._target_snapshot_id(conn=conn, reflection_id=reflection_id),
        )

    def target_snapshot_id(self, *, conn, reflection_id: str) -> str:
        return self._target_snapshot_id(conn=conn, reflection_id=reflection_id)

    def _target_snapshot_id(self, *, conn, reflection_id: str) -> str:
        reflection = self.get_state(reflection_id=reflection_id, conn=conn)
        return review_snapshot_id(target_type="reflection", target=reflection)

    # ---- review return routing ----

    def send_back_to_reflecting(self, *, conn, reflection_id: str, revision_context: str) -> None:
        """Rejection back to the fan-out: the attempt bumps, so every roster
        lens must submit a fresh reflection before synthesizing again."""
        row = self._require_in_review(conn=conn, reflection_id=reflection_id)
        conn.execute(
            """
            UPDATE reflections
            SET status = 'reflecting', attempt_index = attempt_index + 1,
                revision_context = ?, updated_at = ?
            WHERE id = ?
            """,
            (revision_context, now_iso(), reflection_id),
        )
        self.store.record_event(
            conn=conn,
            project_id=row["project_id"],
            event_type="reflection.returned_to_reflecting",
            target_type="reflection",
            target_id=reflection_id,
            payload={"revision_context": revision_context},
        )

    def send_back_to_synthesizing(self, *, conn, reflection_id: str, revision_context: str) -> None:
        """Rejection back to reflection-artifact revision only: the reflections stand, so the
        attempt is NOT bumped — the orchestrator revises the project graph
        reflection document, and/or change spec and resubmits."""
        row = self._require_in_review(conn=conn, reflection_id=reflection_id)
        conn.execute(
            "UPDATE reflections SET status = 'synthesizing', revision_context = ?, updated_at = ? WHERE id = ?",
            (revision_context, now_iso(), reflection_id),
        )
        self.store.record_event(
            conn=conn,
            project_id=row["project_id"],
            event_type="reflection.returned_to_synthesizing",
            target_type="reflection",
            target_id=reflection_id,
            payload={"revision_context": revision_context},
        )

    def _require_in_review(self, *, conn, reflection_id: str):
        row = conn.execute(
            "SELECT * FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"reflection not found: {reflection_id}")
        if row["status"] != "reflection_review":
            raise WorkflowError(
                f"reflection wave is {row['status']!r}; only a wave under "
                "reflection review can be sent back"
            )
        return row

    # ---- reflection drift ----

    def reflection_signal(self, *, project_id: str, conn=None) -> dict[str, Any]:
        """How far project state has drifted from the last published reflection.

        Computed on read, never stored. The output backs the soft 'Consider
        running a project reflection' nudge, the Home coverage badge, and the
        hard experiment.create block once project reflection debt reaches the
        blocking threshold.
        """
        owns_conn = conn is None
        if conn is None:
            conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            terminal = ", ".join(
                f"'{s}'" for s in sorted(EXPERIMENT_TERMINAL_STATUSES)
            )
            current_terminal = {
                str(row["id"]): str(row["status"])
                for row in conn.execute(
                    f"SELECT id, status FROM experiments WHERE project_id = ? AND status IN ({terminal})",
                    (project_id,),
                ).fetchall()
            }
            current_claims = {
                str(row["id"]): str(row["status"])
                for row in conn.execute(
                    "SELECT id, status FROM claims WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            }
            published = self.latest_published(conn=conn, project_id=project_id)
            open_wave = self.open_reflection(conn=conn, project_id=project_id)
            return reflection_signal_state(
                current_terminal=current_terminal,
                current_claims=current_claims,
                published=published,
                open_wave=open_wave,
            )
        finally:
            if owns_conn:
                conn.close()
