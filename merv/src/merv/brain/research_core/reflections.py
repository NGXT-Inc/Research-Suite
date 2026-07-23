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

from contextlib import closing
import json
from typing import Any

from merv.shared.artifact_roles import PROJECT_GRAPH_ROLES

from .domain.experiment_names import validate_experiment_name
from .domain.experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    active_experiment_cap_would_exceed_message,
)
from .domain.graph_lint import graph_problems
from .domain.gates import RoleRequirement
from .domain.reflection_artifacts import (
    claim_refs,
    current_reflection_requirement_artifact,
    graph_diff,
    graph_diff_summary,
    parse_change_spec,
    reflection_coverage_for,
    reflection_doc_review_problems,
    reflection_requirement_roles,
    validate_reflection_roster,
)
from .domain.reflection_policy import (
    covered_terminal_ids,
    reflection_signal_state,
)
from .domain.artifact_evidence import (
    preferred_associated_artifact,
    artifact_state_record,
)
from ..artifacts.ports import EvidenceReader, SubmittedDocument
from .domain.review_snapshot import review_snapshot_id
from .domain.reflection_gates import (
    REFLECTION_GATE_TABLE,
    REFLECTION_TERMINAL_STATUSES,
    allowed_reflection_transitions_for,
)
from .domain.vocabulary import EXPERIMENT_TERMINAL_STATUSES
from .gate_evaluation import (
    GateEvaluation,
    GateItem,
    RequirementEvaluation,
    evaluate_artifact_requirement,
)
from ..kernel.ports.reflection_writers import (
    ReflectionClaimWriter,
    ReflectionExperimentWriter,
)
from ..kernel.state.store import (
    BaseStateStore,
    next_created_seq,
    row_to_dict,
    rows_to_dicts,
)
from ..kernel.utils import NotFoundError, WorkflowError, new_id, now_iso
from .review_gate import evaluate_review_gate


class ReflectionService:
    def __init__(
        self,
        *,
        store: BaseStateStore,
        claims: ReflectionClaimWriter,
        experiment_writer: ReflectionExperimentWriter,
        evidence_reader: EvidenceReader,
    ) -> None:
        self.store = store
        self.claims = claims
        self.experiment_writer = experiment_writer
        self.evidence_reader = evidence_reader

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
        return self.get_state_with_gate(
            reflection_id=reflection_id, project_id=project_id, conn=conn
        )[0]

    def get_state_with_gate(
        self, *, reflection_id: str, project_id: str | None = None, conn=None
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
            data["artifacts"] = [
                artifact_state_record(evidence)
                for evidence in self.evidence_reader.artifacts_for_target(
                    target_type="reflection", target_id=reflection_id
                )
            ]
            data["current_attempt_artifacts"] = [
                res
                for res in data["artifacts"]
                if res.get("attempt_index") == data["attempt_index"]
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
            data["reflection_coverage"] = reflection_coverage_for(reflection=data)
            data["project_graph_diff"] = self._project_graph_diff(
                conn=conn, reflection=data
            )
            evaluation = self._evaluate_gate(conn=conn, reflection=data)
            data["gate_checklist"] = evaluation.checklist()
            data["allowed_transitions"] = [
                dict(item) for item in evaluation.legal_transitions
            ]
            return data, evaluation
        finally:
            if owns_conn:
                conn.close()

    def list_reflections(self, *, project_id: str | None = None) -> dict[str, Any]:
        with closing(self.store.connect()) as conn:
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

    def overview(self, *, project_id: str | None = None) -> dict[str, Any]:
        """All waves plus the current reflection signal for project UI views."""
        with closing(self.store.connect()) as conn:
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

    def project_logic_graph_selection(self, *, project_id: str) -> dict[str, Any]:
        """Select the current project graph wave and reflection signal.

        The UI prefers the open wave's graph while the wave is open,
        falling back to the latest published graph when the open wave has not
        submitted one yet. The transport layer owns response shaping; this
        service owns the record reads and selection policy.
        """
        with closing(self.store.connect()) as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            signal = self.reflection_signal(project_id=project_id, conn=conn)
            reflection = self.open_reflection(conn=conn, project_id=project_id)
            graph_artifact = self._project_graph_artifact(reflection=reflection)
            if reflection is None or graph_artifact is None:
                published = self.latest_published(conn=conn, project_id=project_id)
                published_graph = self._project_graph_artifact(reflection=published)
                if published is not None and published_graph is not None:
                    reflection = published
                    graph_artifact = published_graph
            return {
                "signal": signal,
                "reflection": reflection,
                "graph_artifact": graph_artifact,
            }

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
    def _project_graph_artifact(
        *, reflection: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if reflection is None:
            return None
        return preferred_associated_artifact(
            artifacts=reflection.get("artifacts", []),
            attempt=reflection.get("attempt_index"),
            roles=PROJECT_GRAPH_ROLES,
        )

    def _project_graph_diff(
        self, *, conn, reflection: dict[str, Any]
    ) -> dict[str, Any]:
        current_artifact = self._project_graph_artifact(reflection=reflection)
        # published_graph_version_id holds the artifact id pinned at publish.
        current_artifact_id = str(
            (
                reflection.get("published_graph_version_id")
                if reflection.get("status") == "published"
                else None
            )
            or (current_artifact or {}).get("id")
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
            "current_graph_version_id": current_artifact_id or None,
            "problems": [],
        }
        if not current_artifact_id:
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
            artifact_id=str(base["graph_version_id"]),
            what="previous project logic graph",
        )
        current_graph, current_problems = self._load_graph_for_diff(
            artifact_id=current_artifact_id,
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
        self, *, artifact_id: str, what: str
    ) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            text = self.evidence_reader.submitted_document(
                artifact_id=artifact_id, what=what
            ).text
        except WorkflowError as exc:
            return None, [str(exc)]
        problems = graph_problems(text)
        if problems:
            return None, [f"{what}: {problem}" for problem in problems]
        data = json.loads(text)
        return data, []

    def _evaluate_gate(self, *, conn, reflection: dict[str, Any]) -> GateEvaluation:
        """Collect reflection facts once for enforcement, state, and guidance."""
        status = str(reflection.get("status") or "")
        forward = REFLECTION_GATE_TABLE.get(status)
        requirements: list[RequirementEvaluation] = []
        if forward is not None and status == "reflecting":
            requirements.append(
                self._evaluate_roster_gate(
                    conn=conn,
                    reflection=reflection,
                    requirement=forward.requirements[0],
                )
            )
        elif forward is not None:
            for requirement in forward.requirements:
                artifact = current_reflection_requirement_artifact(
                    reflection=reflection, role=requirement.role
                )
                present = artifact is not None
                problems: tuple[str, ...] = ()
                if present and requirement.validator:
                    try:
                        self._run_validator(
                            conn=conn, reflection=reflection, name=requirement.validator
                        )
                    except WorkflowError as exc:
                        problems = (str(exc),)
                requirements.append(
                    evaluate_artifact_requirement(
                        requirement,
                        present=present,
                        problems=problems,
                        artifact_fields=(
                            None
                            if artifact is None
                            else {
                                "path": artifact.get("path"),
                                "artifact_id": artifact.get("id"),
                                "submitted_role": artifact.get("role"),
                            }
                        ),
                    )
                )

        review = (
            None
            if forward is None or forward.review is None
            else evaluate_review_gate(
                conn=conn,
                target_type="reflection",
                target=reflection,
                review=forward.review,
            )
        )
        return GateEvaluation(
            subject="reflection wave",
            status=status,
            transition=None if forward is None else forward.name,
            leads_to=None if forward is None else forward.to_status,
            terminal=status in REFLECTION_TERMINAL_STATUSES,
            requirements=tuple(requirements),
            review=review,
            legal_transitions=tuple(
                dict(item) for item in allowed_reflection_transitions_for(status)
            ),
        )

    def _evaluate_roster_gate(
        self,
        *,
        conn,
        reflection: dict[str, Any],
        requirement: RoleRequirement,
    ) -> RequirementEvaluation:
        coverage = reflection.get("reflection_coverage") or {}
        by_lens = {
            str(item.get("lens_id") or ""): item
            for item in coverage.get("lenses") or []
        }
        missing_lenses = list(coverage.get("missing") or [])
        role_aliases = set(reflection_requirement_roles(role=requirement.role))
        has_association = any(
            item.get("role") in role_aliases
            for item in reflection.get("current_attempt_artifacts") or []
        )
        missing_error = ""
        if missing_lenses:
            missing_error = requirement.error if not has_association else (
                "reflections are missing for lens(es): "
                + ", ".join(missing_lenses)
                + " — each roster lens must have its own reflection submitted "
                "(artifact.submit with role 'reflection_lens_doc' and its "
                "lens_id) for the current attempt, by its own subagent"
            )
        invalid: dict[str, str] = {}
        if not missing_lenses:
            for lens in coverage.get("lenses") or []:
                lens_id, path = str(lens["lens_id"]), str(lens["path"])
                try:
                    text = self.evidence_reader.submitted_document(
                        artifact_id=str(lens.get("artifact_id") or ""),
                        what=f"reflection {lens_id!r}",
                    ).text
                    if not text.strip():
                        invalid[lens_id] = (
                            f"reflection for lens {lens_id!r} ({path}) is empty — "
                            "write it and resubmit it (artifact.submit) to submit the content"
                        )
                except WorkflowError as exc:
                    invalid[lens_id] = str(exc)

        items: list[GateItem] = []
        for lens in reflection.get("roster") or []:
            lens_id = str(lens.get("id") or "")
            found = by_lens.get(lens_id) or {}
            covered = bool(found.get("covered"))
            problem = invalid.get(lens_id, "")
            item: GateItem = {
                "id": f"reflection_lens:{lens_id}",
                "kind": "reflection_lens",
                "role": requirement.role,
                "lens_id": lens_id,
                "label": f"{str(lens.get('title') or lens_id)} reflection submitted",
                "satisfied": covered and not problem,
                "status": "invalid" if problem else "present" if covered else "missing",
                "gate": requirement.gate,
            }
            if covered:
                item.update(
                    path=found.get("path"),
                    artifact_id=found.get("artifact_id"),
                    submitted_role=found.get("role"),
                )
            else:
                item["missing"] = (
                    f"reflection doc for lens {lens_id!r} "
                    "(artifact.submit with role 'reflection_lens_doc', "
                    f"lens_id {lens_id!r})"
                )
            if problem:
                item["problems"] = [problem]
            items.append(item)
        problems = tuple(invalid.values())
        error = missing_error or (problems[0] if problems else "")
        status = "missing" if missing_lenses else "invalid" if problems else "valid"
        return RequirementEvaluation(
            role=requirement.role,
            status=status,
            blocker_code=(
                ""
                if not error
                else requirement.gate
                if missing_lenses
                else f"{requirement.role}_invalid"
            ),
            enforcement_error=error,
            problems=problems,
            items=tuple(items),
        )

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
            reflection, gate = self.get_state_with_gate(
                reflection_id=reflection_id, project_id=project_id, conn=conn
            )
            status = reflection["status"]
            next_status = gate.require_transition(transition)
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
                        self._current_graph_version_id(
                            reflection=reflection
                        ),
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

    def _run_validator(self, *, conn, reflection: dict[str, Any], name: str) -> None:
        if name == "graph":
            self._validate_project_graph(conn=conn, reflection=reflection)
        elif name in {"reflection_doc", "synthesis_doc"}:
            self._validate_reflection_doc(conn=conn, reflection=reflection)
        elif name == "change_spec":
            self._validate_change_spec(conn=conn, reflection=reflection)

    def _validate_project_graph(self, *, conn, reflection: dict[str, Any]) -> None:
        document = self._submitted_role_document(
            reflection=reflection,
            roles=PROJECT_GRAPH_ROLES,
            what="project logic graph",
        )
        if document is None:
            raise WorkflowError(
                "a project logic graph artifact must be submitted before reflection review"
            )
        problems = graph_problems(document.text)
        if problems:
            raise WorkflowError(
                "project logic graph is not ready for reflection review: "
                + "; ".join(problems)
                + ". Fix the file and resubmit it (artifact.submit) — "
                "see skills/research-workflow/graph-template.md."
            )

    def _validate_reflection_doc(self, *, conn, reflection: dict[str, Any]) -> None:
        document = self._submitted_role_document(
            reflection=reflection,
            roles=("reflection_doc", "synthesis_doc"),
            what="reflection document",
        )
        if document is None:
            raise WorkflowError(
                "a reflection document artifact must be submitted before reflection review"
            )
        problems = reflection_doc_review_problems(
            text=document.text,
            submitted_images=set(document.figure_links),
            path=document.path,
        )
        if problems:
            raise WorkflowError(
                "reflection document is not ready for review: "
                + "; ".join(problems)
                + ". Keep it concise, fix the file, and resubmit it (artifact.submit) to "
                "submit the revision — see "
                "skills/project-reflection/reflection-artifacts-template.md."
            )

    def _validate_change_spec(self, *, conn, reflection: dict[str, Any]) -> None:
        document = self._submitted_role_document(
            reflection=reflection,
            roles=("change_spec",),
            what="change spec",
        )
        if document is None:
            raise WorkflowError(
                "a change spec artifact must be submitted before reflection review"
            )
        self._parse_change_spec(
            conn=conn,
            project_id=str(reflection["project_id"]),
            text=document.text,
            path=document.path,
        )

    def _current_change_spec(
        self, *, conn, reflection: dict[str, Any]
    ) -> dict[str, Any]:
        document = self._submitted_role_document(
            reflection=reflection,
            roles=("change_spec",),
            what="change spec",
        )
        if document is None:
            raise WorkflowError(
                "a change spec artifact must be submitted before publish"
            )
        return self._parse_change_spec(
            conn=conn,
            project_id=str(reflection["project_id"]),
            text=document.text,
            path=document.path,
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
        terminal = ", ".join(
            f"'{status}'" for status in sorted(EXPERIMENT_TERMINAL_STATUSES)
        )
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
            claim_ids = [key_to_claim_id.get(ref, ref) for ref in claim_refs(proposal)]
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

    def _current_graph_version_id(
        self, *, reflection: dict[str, Any]
    ) -> str | None:
        """The current project-graph ARTIFACT id, pinned at publish."""
        artifact = preferred_associated_artifact(
            artifacts=reflection.get("current_attempt_artifacts") or [],
            attempt=reflection.get("attempt_index"),
            roles=PROJECT_GRAPH_ROLES,
        )
        artifact_id = (artifact or {}).get("id")
        return str(artifact_id) if artifact_id else None

    def _submitted_role_document(
        self,
        *,
        reflection: dict[str, Any],
        roles: tuple[str, ...],
        what: str,
    ) -> SubmittedDocument | None:
        artifact = preferred_associated_artifact(
            artifacts=reflection.get("current_attempt_artifacts") or [],
            attempt=reflection.get("attempt_index"),
            roles=roles,
        )
        if artifact is None:
            return None
        return self.evidence_reader.submitted_document(
            artifact_id=str(artifact.get("id") or ""), what=what
        )

    def target_snapshot_id(self, *, conn, reflection_id: str) -> str:
        reflection = self.get_state(reflection_id=reflection_id, conn=conn)
        return review_snapshot_id(target_type="reflection", target=reflection)

    # ---- review return routing ----

    def send_back_to_reflecting(
        self, *, conn, reflection_id: str, revision_context: str
    ) -> None:
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

    def send_back_to_synthesizing(
        self, *, conn, reflection_id: str, revision_context: str
    ) -> None:
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
            terminal = ", ".join(f"'{s}'" for s in sorted(EXPERIMENT_TERMINAL_STATUSES))
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
