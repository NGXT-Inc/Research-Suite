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
import re
from typing import Any

from ..state.blobs import BlobStore
from ..state.store import StateStore, next_created_seq, row_to_dict, rows_to_dicts
from ..utils import NotFoundError, ValidationError, WorkflowError, new_id, now_iso
from .artifacts import markdown_image_links
from .experiment_names import validate_experiment_name
from .graph_lint import graph_problems
from ..domain.vocabulary import (
    CLAIM_CONFIDENCES,
    CLAIM_STATUSES,
    PROJECT_GRAPH_ROLES,
    REFLECTION_LENS_DOC_ROLES,
)
from .pinned import pinned_text_for_version, resubmit_hint
from .reflection_policy import (
    REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
    REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD,
)
from .synthesis_gates import (
    CORE_LENSES,
    CORE_LENS_IDS,
    ROSTER_SIZE,
    SYNTHESIS_GATE_TABLE,
    SYNTHESIS_TERMINAL_STATUSES,
    allowed_synthesis_transitions_for,
)
from .workflow_gates import TERMINAL_STATUSES as EXPERIMENT_TERMINAL_STATUSES


_LENS_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_CHANGE_SPEC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
CHANGE_SPEC_SCHEMA_VERSION = 1
MAX_SYNTHESIS_DOC_BYTES = 16_000
REQUIRED_SYNTHESIS_DOC_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Summary", "summary"),
    ("Critical reading", "critical"),
    ("Decision / future directions", "decision"),
)

class SynthesisService:
    def __init__(
        self,
        *,
        store: StateStore,
        blobs: BlobStore | None = None,
    ) -> None:
        self.store = store
        # Gate lints read submitted (pinned) bytes from here, never the
        # working tree (see services/pinned.py).
        self.blobs = blobs

    # ---- create ----

    def create(
        self,
        *,
        title: str = "",
        lenses: list[dict[str, Any]] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        roster = self._validate_roster(lenses=lenses or [])
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            open_row = conn.execute(
                """
                SELECT id, status FROM syntheses
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
            synthesis_id = new_id(prefix="syn")
            now = now_iso()
            corpus = self._corpus_snapshot(conn=conn, project_id=project_id)
            conn.execute(
                """
                INSERT INTO syntheses
                  (id, project_id, title, status, attempt_index, revision_context,
                   roster_json, corpus_json, created_at, updated_at, created_seq)
                VALUES (?, ?, ?, 'reflecting', 1, '', ?, ?, ?, ?, ?)
                """,
                (
                    synthesis_id,
                    project_id,
                    title.strip(),
                    json.dumps(roster, sort_keys=True),
                    json.dumps(corpus, sort_keys=True),
                    now,
                    now,
                    next_created_seq(conn=conn, table="syntheses"),
                ),
            )
            self.store.record_event(
                conn=conn,
                project_id=project_id,
                event_type="synthesis.created",
                target_type="synthesis",
                target_id=synthesis_id,
                payload={
                    "title": title.strip(),
                    "lenses": [lens["id"] for lens in roster],
                    "corpus_terminal_experiments": len(corpus["terminal_experiments"]),
                },
            )
            return self.get_state(synthesis_id=synthesis_id, conn=conn)

    def _validate_roster(self, *, lenses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Envelope check on the declared roster: exactly 5 unique lenses — the
        3 core ids plus 2 wave-authored ones, each authored lens carrying a
        charter and a stated reason it is distinct. Whether the authored lenses
        are *actually* distinct is judged by the reviewer, not here."""
        contract = (
            "the reflection roster must declare exactly "
            f"{ROSTER_SIZE} lenses: the {len(CORE_LENS_IDS)} core lenses "
            f"({', '.join(CORE_LENS_IDS)}) plus "
            f"{ROSTER_SIZE - len(CORE_LENS_IDS)} lenses you design for this "
            "project, each with a 'charter' and a 'why_distinct' stating how it "
            "differs from the core three and from each other"
        )
        if len(lenses) != ROSTER_SIZE:
            raise ValidationError(f"got {len(lenses)} lenses; {contract}")
        core_by_id = {lens["id"]: lens for lens in CORE_LENSES}
        roster: list[dict[str, Any]] = []
        seen: set[str] = set()
        for lens in lenses:
            lens_id = str(lens.get("id") or "").strip()
            if not _LENS_ID_RE.match(lens_id):
                raise ValidationError(
                    f"invalid lens id {lens_id!r}: use a lowercase slug "
                    "(letters, digits, '_', '-') — it doubles as the reflection "
                    "filename (<lens_id>.md)"
                )
            if lens_id in seen:
                raise ValidationError(f"duplicate lens id: {lens_id}")
            seen.add(lens_id)
            charter = str(lens.get("charter") or "").strip()
            why = str(lens.get("why_distinct") or "").strip()
            core = core_by_id.get(lens_id)
            if core is not None:
                roster.append(
                    {
                        "id": lens_id,
                        "title": str(lens.get("title") or "").strip() or core["title"],
                        "charter": charter or core["charter"],
                        "core": True,
                        "why_distinct": why,
                    }
                )
                continue
            if not charter:
                raise ValidationError(
                    f"lens {lens_id!r} needs a charter (what angle it reads the "
                    f"project from); {contract}"
                )
            if not why:
                raise ValidationError(
                    f"lens {lens_id!r} needs why_distinct (how it differs from "
                    f"the core three and the other authored lens); {contract}"
                )
            roster.append(
                {
                    "id": lens_id,
                    "title": str(lens.get("title") or "").strip()
                    or lens_id.replace("_", " ").replace("-", " "),
                    "charter": charter,
                    "core": False,
                    "why_distinct": why,
                }
            )
        missing_core = [cid for cid in CORE_LENS_IDS if cid not in seen]
        if missing_core:
            raise ValidationError(
                f"missing core lens(es): {', '.join(missing_core)}; {contract}"
            )
        return roster

    def _corpus_snapshot(self, *, conn, project_id: str) -> dict[str, Any]:
        terminal = ", ".join(f"'{s}'" for s in sorted(EXPERIMENT_TERMINAL_STATUSES))
        exp_rows = conn.execute(
            f"""
            SELECT id, attempt_index, status FROM experiments
            WHERE project_id = ? AND status IN ({terminal})
            ORDER BY created_at
            """,
            (project_id,),
        ).fetchall()
        claim_rows = conn.execute(
            "SELECT id, status FROM claims WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return {
            "captured_at": now_iso(),
            "terminal_experiments": rows_to_dicts(rows=exp_rows),
            "claims": rows_to_dicts(rows=claim_rows),
        }

    # ---- read ----

    def get_state(
        self, *, synthesis_id: str, project_id: str | None = None, conn=None
    ) -> dict[str, Any]:
        owns_conn = conn is None
        if conn is None:
            conn = self.store.connect()
        try:
            if owns_conn:
                project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            row = conn.execute(
                "SELECT * FROM syntheses WHERE id = ?", (synthesis_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"synthesis not found: {synthesis_id}")
            data = row_to_dict(row=row) or {}
            if project_id is not None and data["project_id"] != project_id:
                raise NotFoundError(
                    f"synthesis not found in project {project_id}: {synthesis_id}"
                )
            data["roster"] = json.loads(str(data.pop("roster_json", "[]")))
            data["corpus"] = json.loads(str(data.pop("corpus_json", "{}")))
            resource_rows = conn.execute(
                """
                SELECT r.*, a.role AS association_role, a.attempt_index AS association_attempt_index,
                       a.version_id AS association_version_id, a.created_seq AS association_rowid
                FROM resources r
                JOIN resource_associations a ON a.resource_id = r.id
                WHERE a.target_type = 'synthesis' AND a.target_id = ?
                ORDER BY a.attempt_index, a.role, r.path
                """,
                (synthesis_id,),
            ).fetchall()
            data["resources"] = rows_to_dicts(rows=resource_rows)
            data["current_attempt_resources"] = [
                res
                for res in data["resources"]
                if res.get("association_attempt_index") == data["attempt_index"]
            ]
            claim_rows = conn.execute(
                """
                SELECT sc.synthesis_id, sc.claim_id, sc.op, sc.claim_key,
                       sc.created_at, c.statement, c.status, c.confidence
                FROM synthesis_claim_changes sc
                JOIN claims c ON c.id = sc.claim_id
                WHERE sc.synthesis_id = ?
                ORDER BY sc.created_at, sc.claim_id
                """,
                (synthesis_id,),
            ).fetchall()
            data["materialized_claims"] = rows_to_dicts(rows=claim_rows)
            experiment_rows = conn.execute(
                """
                SELECT se.synthesis_id, se.experiment_id, se.proposal_key,
                       se.created_at, e.name, e.intent, e.status
                FROM synthesis_experiments se
                JOIN experiments e ON e.id = se.experiment_id
                WHERE se.synthesis_id = ?
                ORDER BY se.created_at, se.experiment_id
                """,
                (synthesis_id,),
            ).fetchall()
            data["materialized_experiments"] = rows_to_dicts(rows=experiment_rows)
            review_rows = conn.execute(
                """
                SELECT * FROM reviews
                WHERE target_type = 'synthesis' AND target_id = ?
                ORDER BY created_seq DESC
                """,
                (synthesis_id,),
            ).fetchall()
            reviews = rows_to_dicts(rows=review_rows)
            for review in reviews:
                review["findings"] = json.loads(review.pop("findings_json", "[]"))
                review["evidence"] = json.loads(review.pop("evidence_json", "{}"))
            data["reviews"] = reviews
            data["reflection_coverage"] = self._reflection_coverage(synthesis=data)
            data["allowed_transitions"] = allowed_synthesis_transitions_for(
                str(data.get("status", ""))
            )
            return data
        finally:
            if owns_conn:
                conn.close()

    def list_syntheses(self, *, project_id: str | None = None) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT id FROM syntheses WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
            return {
                "syntheses": [
                    self.get_state(synthesis_id=row["id"], conn=conn) for row in rows
                ]
            }
        finally:
            conn.close()

    def open_synthesis(self, *, conn, project_id: str) -> dict[str, Any] | None:
        """The one non-terminal wave for the project, fully hydrated, or None."""
        row = conn.execute(
            """
            SELECT id FROM syntheses
            WHERE project_id = ? AND status NOT IN ('published', 'abandoned')
            ORDER BY created_seq DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_state(synthesis_id=row["id"], conn=conn)

    def latest_published(self, *, conn, project_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT id FROM syntheses
            WHERE project_id = ? AND status = 'published'
            ORDER BY published_at DESC, created_seq DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_state(synthesis_id=row["id"], conn=conn)

    def _reflection_coverage(self, *, synthesis: dict[str, Any]) -> dict[str, Any]:
        """Which roster lenses have a current-attempt reflection associated.

        A reflection covers lens L when its file is named ``<L>.md`` (any
        directory) — the dumb, predictable convention each fan-out subagent is
        told to follow.
        """
        stems: dict[str, dict[str, Any]] = {}
        for res in synthesis.get("current_attempt_resources", []):
            if (
                res.get("association_role") not in REFLECTION_LENS_DOC_ROLES
                or res.get("missing")
            ):
                continue
            path = str(res.get("path") or "")
            name = path.rsplit("/", 1)[-1]
            stem = name.rsplit(".", 1)[0] if "." in name else name
            stems.setdefault(
                stem,
                {
                    "path": path,
                    "version_id": res.get("association_version_id"),
                    "role": res.get("association_role"),
                },
            )
        lenses = []
        missing = []
        for lens in synthesis.get("roster", []):
            lens_id = str(lens.get("id") or "")
            entry = stems.get(lens_id)
            lenses.append(
                {
                    "lens_id": lens_id,
                    "covered": entry is not None,
                    "path": entry["path"] if entry else None,
                    "version_id": entry.get("version_id") if entry else None,
                    "role": entry.get("role") if entry else None,
                }
            )
            if entry is None:
                missing.append(lens_id)
        return {"lenses": lenses, "missing": missing, "complete": not missing}

    # ---- transitions ----

    def transition(
        self,
        *,
        synthesis_id: str,
        transition: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            synthesis = self.get_state(
                synthesis_id=synthesis_id, project_id=project_id, conn=conn
            )
            status = synthesis["status"]
            next_status = self._next_status(
                conn=conn, synthesis=synthesis, transition=transition
            )
            now = now_iso()
            if transition == "publish":
                self._materialize_change_spec(conn=conn, synthesis=synthesis)
                conn.execute(
                    """
                    UPDATE syntheses
                    SET status = ?, published_at = ?, published_graph_version_id = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_status,
                        now,
                        self._current_graph_version_id(conn=conn, synthesis=synthesis),
                        now,
                        synthesis_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE syntheses SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status, now, synthesis_id),
                )
            self.store.record_event(
                conn=conn,
                project_id=synthesis["project_id"],
                event_type="synthesis.transitioned",
                target_type="synthesis",
                target_id=synthesis_id,
                payload={"from": status, "to": next_status, "transition": transition},
            )
            return self.get_state(synthesis_id=synthesis_id, conn=conn)

    def _next_status(self, *, conn, synthesis: dict[str, Any], transition: str) -> str:
        status = str(synthesis["status"])
        if status in SYNTHESIS_TERMINAL_STATUSES:
            raise WorkflowError(
                f"reflection wave is {status!r}; no transitions are allowed from a "
                "terminal state"
            )
        if transition == "abandon":
            return "abandoned"
        forward = SYNTHESIS_GATE_TABLE.get(status)
        if forward is None or forward.name != transition:
            options = ", ".join(
                t["transition"] for t in allowed_synthesis_transitions_for(status)
            )
            raise WorkflowError(
                f"transition {transition!r} is not allowed from {status!r}; "
                f"allowed from here: {options}"
            )
        for requirement in forward.requirements:
            if not self._has_resource_role(
                conn=conn, synthesis_id=synthesis["id"], role=requirement.role
            ):
                raise WorkflowError(requirement.error)
            self._run_validator(
                conn=conn, synthesis=synthesis, name=requirement.validator
            )
        if forward.review is not None and not self._has_passing_review(
            conn=conn, synthesis_id=synthesis["id"], role=forward.review.role
        ):
            raise WorkflowError(forward.review.error)
        return forward.to_status

    def _has_resource_role(self, *, conn, synthesis_id: str, role: str) -> bool:
        roles = (role,)
        if role == "reflection_doc":
            roles = ("reflection_doc", "synthesis_doc")
        elif role == "reflection_lens_doc":
            roles = REFLECTION_LENS_DOC_ROLES
        elif role == "project_graph":
            roles = PROJECT_GRAPH_ROLES
        placeholders = ",".join("?" * len(roles))
        row = conn.execute(
            f"""
            SELECT 1
            FROM resource_associations
            WHERE target_type = 'synthesis' AND target_id = ? AND role IN ({placeholders})
              AND attempt_index = (SELECT attempt_index FROM syntheses WHERE id = ?)
            LIMIT 1
            """,
            (synthesis_id, *roles, synthesis_id),
        ).fetchone()
        return row is not None

    def _run_validator(self, *, conn, synthesis: dict[str, Any], name: str) -> None:
        if name == "roster":
            self._validate_roster_coverage(conn=conn, synthesis=synthesis)
        elif name == "graph":
            self._validate_project_graph(conn=conn, synthesis=synthesis)
        elif name in {"reflection_doc", "synthesis_doc"}:
            self._validate_reflection_doc(conn=conn, synthesis=synthesis)
        elif name == "change_spec":
            self._validate_change_spec(conn=conn, synthesis=synthesis)

    def _validate_roster_coverage(self, *, conn, synthesis: dict[str, Any]) -> None:
        """The hard 'all lenses before synthesize' requirement: every declared
        lens needs a current-attempt reflection (file named <lens_id>.md) that
        exists and is non-empty on disk. Which insights each reflection holds
        is the synthesizer's and reviewer's business, not the gate's."""
        fresh = self.get_state(synthesis_id=synthesis["id"], conn=conn)
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

    def _validate_project_graph(self, *, conn, synthesis: dict[str, Any]) -> None:
        row = self._current_role_row_for_roles(
            conn=conn, synthesis_id=synthesis["id"], roles=PROJECT_GRAPH_ROLES
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

    def _validate_reflection_doc(self, *, conn, synthesis: dict[str, Any]) -> None:
        row = self._current_role_row_for_roles(
            conn=conn,
            synthesis_id=synthesis["id"],
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
        problems = self._reflection_doc_problems(text)
        submitted_images = {
            str(image["link_path"])
            for image in conn.execute(
                "SELECT link_path FROM report_figures WHERE report_version_id = ?",
                (row["version_id"],),
            ).fetchall()
        }
        for link in markdown_image_links(text):
            if link not in submitted_images:
                problems.append(
                    f"image {link!r} has no submitted content: make sure the "
                    f"file exists next to {row['path']}, then re-associate the "
                    "reflection document to submit it"
                )
        if problems:
            raise WorkflowError(
                "reflection document is not ready for review: "
                + "; ".join(problems)
                + ". Keep it concise, fix the file, and re-associate it to "
                "submit the revision — see "
                "skills/project-reflection/reflection-artifacts-template.md."
            )

    def _reflection_doc_problems(self, text: str) -> list[str]:
        problems: list[str] = []
        stripped = text.strip()
        if not stripped:
            return ["reflection document is empty"]
        size = len(text.encode("utf-8"))
        if size > MAX_SYNTHESIS_DOC_BYTES:
            problems.append(
                f"reflection document is {size} bytes; keep it under "
                f"{MAX_SYNTHESIS_DOC_BYTES}"
            )
        headings = {
            re.sub(r"[^a-z0-9]+", " ", match.group(1).lower()).strip()
            for match in _MD_HEADING_RE.finditer(text)
        }
        for canonical, key in REQUIRED_SYNTHESIS_DOC_SECTIONS:
            if not any(heading.startswith(key) for heading in headings):
                problems.append(f"missing required section: {canonical}")
        return problems

    def _validate_change_spec(self, *, conn, synthesis: dict[str, Any]) -> None:
        row = self._current_role_row(
            conn=conn, synthesis_id=synthesis["id"], role="change_spec"
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
            project_id=str(synthesis["project_id"]),
            text=text,
            path=str(row["path"]),
        )

    def _current_change_spec(self, *, conn, synthesis: dict[str, Any]) -> dict[str, Any]:
        row = self._current_role_row(
            conn=conn, synthesis_id=synthesis["id"], role="change_spec"
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
            project_id=str(synthesis["project_id"]),
            text=text,
            path=str(row["path"]),
        )

    def _parse_change_spec(
        self, *, conn, project_id: str, text: str, path: str
    ) -> dict[str, Any]:
        """Validate the reviewed reflection change spec.

        This is the machine-actionable belief-state update: claim changes plus
        either hard stop or a concrete parallel experiment wave. Substance stays
        with the reflection reviewer; this lint verifies that publish can apply
        the spec deterministically.
        """
        problems: list[str] = []
        if not text.strip():
            raise WorkflowError(
                f"change spec {path!r} is empty — write it and "
                "re-associate to submit the content"
            )
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"change spec {path!r} is not valid JSON: {exc}. "
                "Write the role 'change_spec' artifact from "
                "skills/project-reflection/reflection-artifacts-template.md and "
                "re-associate it."
            ) from exc
        if not isinstance(spec, dict):
            raise WorkflowError(
                f"change spec {path!r} must be a JSON object"
            )
        if spec.get("version") != CHANGE_SPEC_SCHEMA_VERSION:
            problems.append(f"version must be {CHANGE_SPEC_SCHEMA_VERSION}")

        claim_keys = self._validate_claim_changes(
            conn=conn,
            project_id=project_id,
            spec=spec,
            problems=problems,
        )
        self._validate_decision(
            conn=conn,
            project_id=project_id,
            spec=spec,
            claim_keys=claim_keys,
            problems=problems,
        )
        if problems:
            raise WorkflowError(
                "change spec is not ready for review: "
                + "; ".join(problems)
                + ". Fix the file and re-associate it to submit the revision — "
                "see skills/project-reflection/reflection-artifacts-template.md."
            )
        return spec

    def _validate_claim_changes(
        self,
        *,
        conn,
        project_id: str,
        spec: dict[str, Any],
        problems: list[str],
    ) -> dict[str, dict[str, Any]]:
        raw = spec.get("claim_changes", [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            problems.append("claim_changes must be a list")
            return {}
        claim_keys: dict[str, dict[str, Any]] = {}
        updated_claim_ids: set[str] = set()
        for index, change in enumerate(raw):
            label = f"claim_changes[{index}]"
            if not isinstance(change, dict):
                problems.append(f"{label} must be an object")
                continue
            op = str(change.get("op") or "").strip()
            if op not in {"create", "update"}:
                problems.append(f"{label}.op must be 'create' or 'update'")
                continue
            if not str(change.get("rationale") or "").strip():
                problems.append(f"{label} needs a rationale")
            confidence = change.get("confidence")
            if confidence is not None and confidence not in CLAIM_CONFIDENCES:
                problems.append(
                    f"{label}.confidence must be one of {', '.join(sorted(CLAIM_CONFIDENCES))}"
                )
            status = change.get("status")
            if status is not None and status not in CLAIM_STATUSES:
                problems.append(
                    f"{label}.status must be one of {', '.join(sorted(CLAIM_STATUSES))}"
                )
            if op == "create":
                key = str(change.get("key") or "").strip()
                if key:
                    if not _CHANGE_SPEC_KEY_RE.fullmatch(key):
                        problems.append(
                            f"{label}.key must start with a letter and use only "
                            "letters, digits, '_' and '-'"
                        )
                    elif key in claim_keys:
                        problems.append(f"duplicate claim key: {key}")
                    else:
                        claim_keys[key] = change
                if not str(change.get("statement") or "").strip():
                    problems.append(f"{label}.statement is required for create")
            else:
                claim_id = str(change.get("claim_id") or "").strip()
                if not claim_id:
                    problems.append(f"{label}.claim_id is required for update")
                elif claim_id in updated_claim_ids:
                    problems.append(f"duplicate claim update: {claim_id}")
                elif not self._claim_exists(conn=conn, project_id=project_id, claim_id=claim_id):
                    problems.append(f"{label}.claim_id not found in project: {claim_id}")
                else:
                    updated_claim_ids.add(claim_id)
                if not any(
                    field in change
                    for field in ("statement", "scope", "status", "confidence")
                ):
                    problems.append(
                        f"{label} update must include at least one of "
                        "statement, scope, status, confidence"
                    )
        return claim_keys

    def _validate_decision(
        self,
        *,
        conn,
        project_id: str,
        spec: dict[str, Any],
        claim_keys: dict[str, dict[str, Any]],
        problems: list[str],
    ) -> None:
        decision = spec.get("decision")
        if not isinstance(decision, dict):
            problems.append("decision must be an object")
            return
        typ = str(decision.get("type") or "").strip()
        if typ == "hard_stop":
            if not str(decision.get("rationale") or "").strip():
                problems.append("decision.rationale is required for hard_stop")
            active = self._non_terminal_experiments(conn=conn, project_id=project_id)
            if active:
                problems.append(
                    "hard_stop requires no non-terminal experiments; active: "
                    + ", ".join(active)
                )
            return
        if typ != "create_experiments":
            problems.append("decision.type must be 'hard_stop' or 'create_experiments'")
            return
        experiments = decision.get("experiments")
        if not isinstance(experiments, list):
            problems.append("decision.experiments must be a list")
            return
        if len(experiments) < 2:
            problems.append(
                "decision.experiments must contain at least two experiments so "
                "the approved wave can run in parallel"
            )
        if len(experiments) > 3:
            problems.append(
                "decision.experiments must contain no more than three experiments"
            )
        seen_names: set[str] = set()
        for index, proposal in enumerate(experiments):
            label = f"decision.experiments[{index}]"
            if not isinstance(proposal, dict):
                problems.append(f"{label} must be an object")
                continue
            key = str(proposal.get("key") or "").strip()
            if key and not _CHANGE_SPEC_KEY_RE.fullmatch(key):
                problems.append(
                    f"{label}.key must start with a letter and use only "
                    "letters, digits, '_' and '-'"
                )
            name = str(proposal.get("name") or "").strip()
            try:
                name = validate_experiment_name(name)
            except ValidationError as exc:
                problems.append(f"{label}.name invalid: {exc}")
                name = ""
            if name:
                lowered = name.lower()
                if lowered in seen_names:
                    problems.append(f"duplicate experiment name in change spec: {name}")
                seen_names.add(lowered)
                if self._experiment_name_exists(conn=conn, project_id=project_id, name=name):
                    problems.append(f"experiment name already exists in project: {name}")
            if not str(proposal.get("intent") or "").strip():
                problems.append(f"{label}.intent is required")
            if not str(proposal.get("parallelism") or "").strip():
                problems.append(
                    f"{label}.parallelism is required; state why this experiment "
                    "can run independently of the rest of the wave"
                )
            refs = self._claim_refs(proposal)
            if not refs:
                problems.append(f"{label} must reference at least one tested claim")
            for ref in refs:
                if ref in claim_keys:
                    continue
                if not self._claim_exists(conn=conn, project_id=project_id, claim_id=ref):
                    problems.append(f"{label} references unknown claim or claim key: {ref}")

    def _claim_refs(self, proposal: dict[str, Any]) -> list[str]:
        raw = proposal.get("tested_claim_refs", proposal.get("tested_claim_ids", []))
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw.strip()] if raw.strip() else []
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        return []

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
            ORDER BY created_at
            """,
            (project_id,),
        ).fetchall()
        return [str(row["name"] or row["id"]) for row in rows]

    def _materialize_change_spec(self, *, conn, synthesis: dict[str, Any]) -> None:
        """Apply the reviewer-approved belief-state update.

        This is called only from the publish transition after the review gate
        passes. Rejected syntheses never reach this function, so speculative
        claim edits or experiment specs do not leak into project state.
        """
        project_id = str(synthesis["project_id"])
        synthesis_id = str(synthesis["id"])
        spec = self._current_change_spec(conn=conn, synthesis=synthesis)
        key_to_claim_id = self._materialize_claim_changes(
            conn=conn,
            project_id=project_id,
            synthesis_id=synthesis_id,
            changes=spec.get("claim_changes") or [],
        )
        decision = spec["decision"]
        if decision["type"] == "hard_stop":
            self._materialize_hard_stop(
                conn=conn,
                project_id=project_id,
                synthesis_id=synthesis_id,
                rationale=str(decision.get("rationale") or "").strip(),
            )
            return
        self._materialize_experiment_wave(
            conn=conn,
            project_id=project_id,
            synthesis_id=synthesis_id,
            key_to_claim_id=key_to_claim_id,
            experiments=decision.get("experiments") or [],
        )

    def _materialize_claim_changes(
        self,
        *,
        conn,
        project_id: str,
        synthesis_id: str,
        changes: list[dict[str, Any]],
    ) -> dict[str, str]:
        now = now_iso()
        key_to_claim_id: dict[str, str] = {}
        for change in changes:
            op = str(change["op"])
            key = str(change.get("key") or "").strip()
            if op == "create":
                claim_id = new_id(prefix="claim")
                status = str(change.get("status") or "active")
                confidence = str(change.get("confidence") or "medium")
                conn.execute(
                    """
                    INSERT INTO claims
                      (id, project_id, statement, scope, status, confidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        project_id,
                        str(change.get("statement") or "").strip(),
                        str(change.get("scope") or "").strip(),
                        status,
                        confidence,
                        now,
                    ),
                )
                self.store.record_event(
                    conn=conn,
                    project_id=project_id,
                    event_type="claim.created",
                    target_type="claim",
                    target_id=claim_id,
                    payload={
                        "statement": str(change.get("statement") or "").strip(),
                        "source_synthesis_id": synthesis_id,
                        "rationale": str(change.get("rationale") or "").strip(),
                    },
                )
                if key:
                    key_to_claim_id[key] = claim_id
            else:
                claim_id = str(change["claim_id"]).strip()
                row = conn.execute(
                    "SELECT * FROM claims WHERE id = ? AND project_id = ?",
                    (claim_id, project_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"claim not found: {claim_id}")
                next_statement = (
                    str(change["statement"]).strip()
                    if "statement" in change
                    else str(row["statement"])
                )
                next_scope = (
                    str(change["scope"]).strip()
                    if "scope" in change
                    else str(row["scope"])
                )
                next_status = str(change.get("status") or row["status"])
                next_confidence = str(change.get("confidence") or row["confidence"])
                conn.execute(
                    """
                    UPDATE claims
                    SET statement = ?, scope = ?, status = ?, confidence = ?
                    WHERE id = ?
                    """,
                    (next_statement, next_scope, next_status, next_confidence, claim_id),
                )
                self.store.record_event(
                    conn=conn,
                    project_id=project_id,
                    event_type="claim.updated",
                    target_type="claim",
                    target_id=claim_id,
                    payload={
                        "statement": next_statement,
                        "scope": next_scope,
                        "status": next_status,
                        "confidence": next_confidence,
                        "source_synthesis_id": synthesis_id,
                        "rationale": str(change.get("rationale") or "").strip(),
                    },
                )
            conn.execute(
                """
                INSERT INTO synthesis_claim_changes
                  (synthesis_id, claim_id, op, claim_key, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (synthesis_id, claim_id, op, key, now_iso()),
            )
        return key_to_claim_id

    def _materialize_hard_stop(
        self, *, conn, project_id: str, synthesis_id: str, rationale: str
    ) -> None:
        now = now_iso()
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        assignments = [
            "status = 'stopped'",
            "hard_stop_reflection_id = ?",
            "hard_stop_rationale = ?",
            "stopped_at = ?",
        ]
        params: list[Any] = [synthesis_id, rationale, now]
        if "hard_stop_synthesis_id" in columns:
            assignments.insert(2, "hard_stop_synthesis_id = ?")
            params.insert(1, synthesis_id)
        params.append(project_id)
        conn.execute(
            f"""
            UPDATE projects
            SET {", ".join(assignments)}
            WHERE id = ?
            """,
            params,
        )
        self.store.record_event(
            conn=conn,
            project_id=project_id,
            event_type="project.stopped",
            target_type="project",
            target_id=project_id,
            payload={"synthesis_id": synthesis_id, "rationale": rationale},
        )

    def _materialize_experiment_wave(
        self,
        *,
        conn,
        project_id: str,
        synthesis_id: str,
        key_to_claim_id: dict[str, str],
        experiments: list[dict[str, Any]],
    ) -> None:
        for proposal in experiments:
            name = validate_experiment_name(str(proposal.get("name") or ""))
            intent = str(proposal.get("intent") or "").strip()
            claim_ids = [
                key_to_claim_id.get(ref, ref)
                for ref in self._claim_refs(proposal)
            ]
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
            proposal_key = str(proposal.get("key") or "").strip()
            conn.execute(
                """
                INSERT INTO synthesis_experiments
                  (synthesis_id, experiment_id, proposal_key, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (synthesis_id, experiment_id, proposal_key, now_iso()),
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
                    "proposal_key": proposal_key,
                    "parallelism": str(proposal.get("parallelism") or "").strip(),
                },
            )
    def _current_role_row(self, *, conn, synthesis_id: str, role: str):
        return conn.execute(
            """
            SELECT r.path, a.version_id
            FROM resource_associations a
            JOIN resources r ON r.id = a.resource_id
            WHERE a.target_type = 'synthesis' AND a.target_id = ? AND a.role = ?
              AND a.attempt_index = (SELECT attempt_index FROM syntheses WHERE id = ?)
              AND r.deleted = 0
            ORDER BY a.created_seq DESC
            LIMIT 1
            """,
            (synthesis_id, role, synthesis_id),
        ).fetchone()

    def _current_role_row_for_roles(
        self, *, conn, synthesis_id: str, roles: tuple[str, ...]
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
            WHERE a.target_type = 'synthesis' AND a.target_id = ?
              AND a.role IN ({placeholders})
              AND a.attempt_index = (SELECT attempt_index FROM syntheses WHERE id = ?)
              AND r.deleted = 0
            ORDER BY CASE a.role {order_cases} ELSE {len(roles)} END,
                     a.created_seq DESC
            LIMIT 1
            """,
            (synthesis_id, *roles, synthesis_id, *roles),
        ).fetchone()

    def _current_graph_version_id(self, *, conn, synthesis: dict[str, Any]) -> str | None:
        row = self._current_role_row_for_roles(
            conn=conn, synthesis_id=synthesis["id"], roles=PROJECT_GRAPH_ROLES
        )
        return str(row["version_id"]) if row and row["version_id"] else None

    def _pinned_text(
        self, *, conn, version_id: Any, path: str, role: str, what: str
    ) -> str:
        """The submitted bytes of a pinned association, never the working tree."""
        if self.blobs is None:
            raise WorkflowError(
                f"{what}: no blob store is configured; gated artifacts cannot be linted"
            )
        if not version_id:
            raise WorkflowError(
                f"{what} ({path}) has no pinned version — "
                + resubmit_hint(role=role, path=path)
            )
        return pinned_text_for_version(
            conn=conn,
            blobs=self.blobs,
            version_id=str(version_id),
            what=what,
            role=role,
        )

    def _has_passing_review(self, *, conn, synthesis_id: str, role: str) -> bool:
        snapshot_id = self._target_snapshot_id(conn=conn, synthesis_id=synthesis_id)
        row = conn.execute(
            """
            SELECT 1
            FROM reviews
            WHERE target_type = 'synthesis' AND target_id = ? AND role = ?
              AND verdict = 'pass' AND target_snapshot_id = ?
            LIMIT 1
            """,
            (synthesis_id, role, snapshot_id),
        ).fetchone()
        return row is not None

    def target_snapshot_id(self, *, conn, synthesis_id: str) -> str:
        return self._target_snapshot_id(conn=conn, synthesis_id=synthesis_id)

    def _target_snapshot_id(self, *, conn, synthesis_id: str) -> str:
        synthesis = self.get_state(synthesis_id=synthesis_id, conn=conn)
        resource_tokens = [
            f"{res['id']}:{res.get('association_version_id') or res['version_token']}:{res.get('association_role', '')}:{res.get('association_attempt_index', 0)}"
            for res in synthesis.get("current_attempt_resources", [])
        ]
        return "|".join(
            [
                "synthesis",
                synthesis["id"],
                synthesis["status"],
                str(synthesis["attempt_index"]),
                ",".join(sorted(resource_tokens)),
            ]
        )

    # ---- review return routing ----

    def send_back_to_reflecting(self, *, conn, synthesis_id: str, revision_context: str) -> None:
        """Rejection back to the fan-out: the attempt bumps, so every roster
        lens must submit a fresh reflection before synthesizing again."""
        row = self._require_in_review(conn=conn, synthesis_id=synthesis_id)
        conn.execute(
            """
            UPDATE syntheses
            SET status = 'reflecting', attempt_index = attempt_index + 1,
                revision_context = ?, updated_at = ?
            WHERE id = ?
            """,
            (revision_context, now_iso(), synthesis_id),
        )
        self.store.record_event(
            conn=conn,
            project_id=row["project_id"],
            event_type="synthesis.returned_to_reflecting",
            target_type="synthesis",
            target_id=synthesis_id,
            payload={"revision_context": revision_context},
        )

    def send_back_to_synthesizing(self, *, conn, synthesis_id: str, revision_context: str) -> None:
        """Rejection back to reflection-artifact revision only: the reflections stand, so the
        attempt is NOT bumped — the orchestrator revises the project graph
        reflection document, and/or change spec and resubmits."""
        row = self._require_in_review(conn=conn, synthesis_id=synthesis_id)
        conn.execute(
            "UPDATE syntheses SET status = 'synthesizing', revision_context = ?, updated_at = ? WHERE id = ?",
            (revision_context, now_iso(), synthesis_id),
        )
        self.store.record_event(
            conn=conn,
            project_id=row["project_id"],
            event_type="synthesis.returned_to_synthesizing",
            target_type="synthesis",
            target_id=synthesis_id,
            payload={"revision_context": revision_context},
        )

    def _require_in_review(self, *, conn, synthesis_id: str):
        row = conn.execute(
            "SELECT * FROM syntheses WHERE id = ?", (synthesis_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"synthesis not found: {synthesis_id}")
        if row["status"] != "synthesis_review":
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
            open_wave = self.open_synthesis(conn=conn, project_id=project_id)

            if published is None:
                covered_ids: set[str] = set()
                snapshot_claims: dict[str, str] = {}
            else:
                corpus = published.get("corpus") or {}
                covered_ids = {
                    str(exp.get("id"))
                    for exp in corpus.get("terminal_experiments", [])
                }
                snapshot_claims = {
                    str(claim.get("id")): str(claim.get("status"))
                    for claim in corpus.get("claims", [])
                }

            new_terminal = sorted(set(current_terminal) - covered_ids)
            claims_changed = [
                {"id": cid, "from": snapshot_claims.get(cid), "to": status}
                for cid, status in sorted(current_claims.items())
                if published is not None and snapshot_claims.get(cid) != status
            ]
            contradicted_flip = any(
                change["to"] == "contradicted" for change in claims_changed
            )
            experiment_create_blocked = (
                len(new_terminal) >= REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD
            )
            stale = open_wave is None and (
                len(new_terminal) >= REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD
                or contradicted_flip
            )
            signal: dict[str, Any] = {
                "terminal_experiments": len(current_terminal),
                "covered_terminal_experiments": len(covered_ids & set(current_terminal)),
                "new_terminal_since_publish": len(new_terminal),
                "claims_changed_since_publish": len(claims_changed),
                "contradicted_flip": contradicted_flip,
                "last_published_at": (published or {}).get("published_at"),
                "last_published_synthesis_id": (published or {}).get("id"),
                "open_synthesis_id": (open_wave or {}).get("id"),
                "stale": stale,
                "experiment_create_blocked": experiment_create_blocked,
                "nudge_new_terminal_threshold": REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD,
                "block_new_terminal_threshold": REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
            }
            signal["hint"] = self._staleness_hint(signal=signal, published=published)
            return signal
        finally:
            if owns_conn:
                conn.close()

    def _staleness_hint(
        self, *, signal: dict[str, Any], published: dict[str, Any] | None
    ) -> str:
        if not signal["stale"]:
            return ""
        if signal.get("experiment_create_blocked"):
            if published is None:
                return (
                    "Project reflection required before creating another "
                    "experiment — "
                    f"{signal['terminal_experiments']} experiments have finished "
                    "and no project reflection exists yet. Use the "
                    "project-reflection skill (reflection.create) and publish the "
                    "wave before creating another experiment."
                )
            pieces = [
                "Project reflection required before creating another experiment — "
                f"{signal['new_terminal_since_publish']} experiments have finished "
                "since the last published reflection"
            ]
            if signal["claims_changed_since_publish"]:
                changed = f"{signal['claims_changed_since_publish']} claims have changed"
                if signal["contradicted_flip"]:
                    changed += " (including a claim now contradicted)"
                pieces.append(changed)
            pieces.append(
                "the current reflection covers "
                f"{signal['covered_terminal_experiments']} of "
                f"{signal['terminal_experiments']} finished experiments"
            )
            return (
                "; ".join(pieces)
                + ". Publish a project reflection wave before creating another "
                "experiment."
            )
        if published is None:
            return (
                "Consider running the project's first reflection — "
                f"{signal['terminal_experiments']} experiments have finished and "
                "no project reflection exists yet. Use the project-reflection "
                "skill (reflection.create) when you judge the time is right."
            )
        pieces = [
            "Consider running a project reflection — "
            f"{signal['new_terminal_since_publish']} experiments have finished "
            "since the last published reflection"
        ]
        if signal["claims_changed_since_publish"]:
            changed = f"{signal['claims_changed_since_publish']} claims have changed"
            if signal["contradicted_flip"]:
                changed += " (including a claim now contradicted)"
            pieces.append(changed)
        pieces.append(
            "the current reflection covers "
            f"{signal['covered_terminal_experiments']} of "
            f"{signal['terminal_experiments']} finished experiments"
        )
        return (
            "; ".join(pieces)
            + ". Whether these developments change the project's logic state is "
            "your call (project-reflection skill, reflection.create)."
        )
