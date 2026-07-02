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

from ..domain.experiment_names import validate_experiment_name
from ..domain.experiment_policy import (
    ACTIVE_EXPERIMENT_CAP,
    active_experiment_cap_would_exceed_message,
)
from ..domain.graph_lint import graph_problems
from ..domain.markdown_images import markdown_image_links
from ..domain.reflection_policy import (
    REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
    REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD,
    covered_terminal_ids,
)
from ..domain.resource_selection import preferred_associated_resource
from ..domain.review_snapshot import review_snapshot_id
from ..domain.synthesis_gates import (
    CORE_LENSES,
    CORE_LENS_IDS,
    ROSTER_SIZE,
    SYNTHESIS_GATE_TABLE,
    SYNTHESIS_TERMINAL_STATUSES,
    allowed_synthesis_transitions_for,
)
from ..domain.vocabulary import (
    CLAIM_CONFIDENCES,
    CLAIM_STATUSES,
    EXPERIMENT_TERMINAL_STATUSES,
    PROJECT_GRAPH_ROLES,
    REFLECTION_LENS_DOC_ROLES,
)
from ..ports.synthesis_writers import (
    SynthesisClaimWriter,
    SynthesisExperimentWriter,
    SynthesisProjectWriter,
)
from ..state.blobs import BlobStore
from ..state.store import BaseStateStore, next_created_seq, row_to_dict, rows_to_dicts
from ..utils import NotFoundError, ValidationError, WorkflowError, new_id, now_iso
from .pinned import pinned_text_for_version, resubmit_hint


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
        store: BaseStateStore,
        claims: SynthesisClaimWriter,
        experiment_writer: SynthesisExperimentWriter,
        project_writer: SynthesisProjectWriter,
        blobs: BlobStore | None = None,
    ) -> None:
        self.store = store
        self.claims = claims
        self.experiment_writer = experiment_writer
        self.project_writer = project_writer
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
            if data.get("status") == "published" and data["materialized_experiments"]:
                data["post_publish_guidance"] = self._post_publish_guidance(
                    materialized_experiments=data["materialized_experiments"],
                )
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
            data["project_graph_diff"] = self._project_graph_diff(
                conn=conn, synthesis=data
            )
            data["gate_checklist"] = self._gate_checklist(conn=conn, synthesis=data)
            data["allowed_transitions"] = allowed_synthesis_transitions_for(
                str(data.get("status", ""))
            )
            return data
        finally:
            if owns_conn:
                conn.close()

    def _post_publish_guidance(
        self, *, materialized_experiments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        experiments = [
            {
                "experiment_id": row.get("experiment_id"),
                "name": row.get("name"),
                "status": row.get("status"),
                "folder": f"experiments/{row.get('name')}/",
                "intent": row.get("intent"),
            }
            for row in materialized_experiments
        ]
        count = len(experiments)
        noun = "experiment" if count == 1 else "experiments"
        return {
            "summary": (
                f"Reflection publish created {count} planned {noun}. "
                "Materialize their local folders before editing files, then "
                "call workflow.status_and_next for the experiment you start."
            ),
            "experiments": experiments,
            "recommended_actions": [
                {
                    "tool": "experiment.materialize_folders",
                    "arguments": {"status": "planned"},
                    "why": "Create local folders for the newly planned experiment wave.",
                },
                {
                    "tool": "workflow.status_and_next",
                    "arguments": {"experiment_id": experiments[0]["experiment_id"]},
                    "why": "Start with the first newly planned experiment.",
                },
            ],
        }

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

    def overview(self, *, project_id: str | None = None) -> dict[str, Any]:
        """All waves plus the current reflection signal for project UI views."""
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            rows = conn.execute(
                "SELECT id FROM syntheses WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
            syntheses = [
                self.get_state(synthesis_id=row["id"], conn=conn) for row in rows
            ]
            signal = self.reflection_signal(project_id=project_id, conn=conn)
            open_wave = self.open_synthesis(conn=conn, project_id=project_id)
            published = self.latest_published(conn=conn, project_id=project_id)
            return {
                "syntheses": syntheses,
                "current": open_wave or published,
                "open_synthesis": open_wave,
                "latest_published": published,
                "signal": signal,
            }
        finally:
            conn.close()

    def project_logic_graph_selection(self, *, project_id: str) -> dict[str, Any]:
        """Select the current project graph wave and reflection signal.

        The UI prefers the open wave's graph while synthesis is in progress,
        falling back to the latest published graph when the open wave has not
        submitted one yet. The transport layer owns response shaping; this
        service owns the record reads and selection policy.
        """
        conn = self.store.connect()
        try:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            signal = self.reflection_signal(project_id=project_id, conn=conn)
            synthesis = self.open_synthesis(conn=conn, project_id=project_id)
            graph_resource = self._project_graph_resource(synthesis=synthesis)
            if synthesis is None or graph_resource is None:
                published = self.latest_published(conn=conn, project_id=project_id)
                published_graph = self._project_graph_resource(synthesis=published)
                if published is not None and published_graph is not None:
                    synthesis = published
                    graph_resource = published_graph
            return {
                "signal": signal,
                "synthesis": synthesis,
                "graph_resource": graph_resource,
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

    @staticmethod
    def _project_graph_resource(
        *, synthesis: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if synthesis is None:
            return None
        return preferred_associated_resource(
            resources=synthesis.get("resources", []),
            attempt=synthesis.get("attempt_index"),
            roles=PROJECT_GRAPH_ROLES,
        )

    def _project_graph_diff(self, *, conn, synthesis: dict[str, Any]) -> dict[str, Any]:
        current_resource = self._project_graph_resource(synthesis=synthesis)
        current_version_id = str(
            (
                synthesis.get("published_graph_version_id")
                if synthesis.get("status") == "published"
                else None
            )
            or (current_resource or {}).get("association_version_id")
            or ""
        )
        base = self._previous_published_graph_ref(conn=conn, synthesis=synthesis)
        result: dict[str, Any] = {
            "available": False,
            "reason": "",
            "summary": "",
            "base_reflection_id": base.get("reflection_id") if base else None,
            "base_graph_version_id": base.get("graph_version_id") if base else None,
            "current_reflection_id": synthesis.get("id"),
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

        diff = self._diff_graphs(base_graph=base_graph, current_graph=current_graph)
        result.update(diff)
        result["available"] = True
        result["reason"] = ""
        result["summary"] = self._graph_diff_summary(diff=diff)
        return result

    def _previous_published_graph_ref(
        self, *, conn, synthesis: dict[str, Any]
    ) -> dict[str, Any] | None:
        project_id = str(synthesis.get("project_id") or "")
        status = str(synthesis.get("status") or "")
        current_id = str(synthesis.get("id") or "")
        params: tuple[Any, ...]
        if status == "published":
            query = """
                SELECT id, published_graph_version_id
                FROM syntheses
                WHERE project_id = ? AND status = 'published'
                  AND id != ? AND created_seq < ?
                ORDER BY published_at DESC, created_seq DESC
                LIMIT 1
                """
            params = (project_id, current_id, int(synthesis.get("created_seq") or 0))
        else:
            query = """
                SELECT id, published_graph_version_id
                FROM syntheses
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
        if self.blobs is None:
            return None, [f"{what}: no blob store is configured"]
        try:
            text = pinned_text_for_version(
                conn=conn,
                blobs=self.blobs,
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

    def _diff_graphs(
        self, *, base_graph: dict[str, Any], current_graph: dict[str, Any]
    ) -> dict[str, Any]:
        base_nodes = self._graph_node_index(graph=base_graph)
        current_nodes = self._graph_node_index(graph=current_graph)
        base_edges = self._graph_edge_index(graph=base_graph)
        current_edges = self._graph_edge_index(graph=current_graph)
        return {
            "nodes": self._diff_indexed_items(base=base_nodes, current=current_nodes),
            "edges": self._diff_indexed_items(base=base_edges, current=current_edges),
        }

    def _graph_node_index(self, *, graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "")
            if node_id:
                indexed[node_id] = self._sorted_json_object(node)
        return indexed

    def _graph_edge_index(self, *, graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for edge in graph.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            frm = str(edge.get("from") or "")
            to = str(edge.get("to") or "")
            if frm and to:
                indexed[f"{frm}->{to}"] = self._sorted_json_object(edge)
        return indexed

    @staticmethod
    def _sorted_json_object(item: dict[str, Any]) -> dict[str, Any]:
        return {key: item[key] for key in sorted(item)}

    def _diff_indexed_items(
        self, *, base: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        base_keys = set(base)
        current_keys = set(current)
        changed = []
        for key in sorted(base_keys & current_keys):
            before = base[key]
            after = current[key]
            if before == after:
                continue
            changed.append(
                {
                    "id": key,
                    "before": before,
                    "after": after,
                    "changed_fields": [
                        field
                        for field in sorted(set(before) | set(after))
                        if before.get(field) != after.get(field)
                    ],
                }
            )
        return {
            "added": [current[key] for key in sorted(current_keys - base_keys)],
            "removed": [base[key] for key in sorted(base_keys - current_keys)],
            "changed": changed,
            "unchanged_count": len(base_keys & current_keys) - len(changed),
        }

    @staticmethod
    def _graph_diff_summary(*, diff: dict[str, Any]) -> str:
        nodes = diff.get("nodes") or {}
        edges = diff.get("edges") or {}
        return (
            "Project graph diff: "
            f"{len(nodes.get('added') or [])} nodes added, "
            f"{len(nodes.get('removed') or [])} removed, "
            f"{len(nodes.get('changed') or [])} changed; "
            f"{len(edges.get('added') or [])} edges added, "
            f"{len(edges.get('removed') or [])} removed, "
            f"{len(edges.get('changed') or [])} changed."
        )

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

    def _gate_checklist(self, *, conn, synthesis: dict[str, Any]) -> dict[str, Any]:
        """Current reflection-wave gate as machine-readable checklist data.

        This is the reflection counterpart of experiment state gate_checklist:
        it derives from the declarative synthesis gate table, reports exactly
        which lens/artifact/review items are missing or invalid, and uses the
        same pinned-byte validators that transitions use.
        """
        status = str(synthesis.get("status") or "")
        forward = SYNTHESIS_GATE_TABLE.get(status)
        if forward is None:
            return {
                "status": status,
                "transition": None,
                "leads_to": None,
                "ready": status in SYNTHESIS_TERMINAL_STATUSES,
                "items": [],
            }

        if status == "reflecting":
            items = self._reflection_lens_checklist_items(synthesis=synthesis)
        else:
            items = []
            for requirement in forward.requirements:
                resource = self._current_requirement_resource(
                    synthesis=synthesis, role=requirement.role
                )
                present = resource is not None
                problems: list[str] = []
                state = "present" if present else "missing"
                if present and requirement.validator:
                    try:
                        self._run_validator(
                            conn=conn, synthesis=synthesis, name=requirement.validator
                        )
                    except WorkflowError as exc:
                        problems = [str(exc)]
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
            snapshot_id = review_snapshot_id(target_type="synthesis", target=synthesis)
            passed = any(
                row.get("role") == review.role
                and row.get("verdict") == "pass"
                and row.get("target_snapshot_id") == snapshot_id
                for row in synthesis.get("reviews", [])
            )
            request = self._latest_review_request(
                conn=conn,
                synthesis_id=str(synthesis["id"]),
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
                "label": self._gate_review_label(role=review.role),
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

    def _reflection_lens_checklist_items(
        self, *, synthesis: dict[str, Any]
    ) -> list[dict[str, Any]]:
        coverage_by_lens = {
            str(row.get("lens_id") or ""): row
            for row in (synthesis.get("reflection_coverage") or {}).get("lenses", [])
        }
        items: list[dict[str, Any]] = []
        for lens in synthesis.get("roster", []):
            lens_id = str(lens.get("id") or "")
            coverage = coverage_by_lens.get(lens_id) or {}
            covered = bool(coverage.get("covered"))
            title = str(lens.get("title") or lens_id)
            item: dict[str, Any] = {
                "id": f"reflection_lens:{lens_id}",
                "kind": "reflection_lens",
                "role": "reflection_lens_doc",
                "lens_id": lens_id,
                "label": f"{title} reflection submitted",
                "satisfied": covered,
                "status": "present" if covered else "missing",
                "gate": "reflection_roster_incomplete",
                "action": "fan_out_reflection_subagents",
            }
            if covered:
                item["path"] = coverage.get("path")
                item["version_id"] = coverage.get("version_id")
                item["association_role"] = coverage.get("role")
            else:
                item["missing"] = (
                    f"reflection doc for lens {lens_id!r} "
                    "(role 'reflection_lens_doc', file <lens_id>.md)"
                )
            items.append(item)
        return items

    def _current_requirement_resource(
        self, *, synthesis: dict[str, Any], role: str
    ) -> dict[str, Any] | None:
        return preferred_associated_resource(
            resources=synthesis.get("current_attempt_resources") or [],
            attempt=synthesis.get("attempt_index"),
            roles=self._roles_for_requirement(role=role),
        )

    @staticmethod
    def _roles_for_requirement(*, role: str) -> tuple[str, ...]:
        if role == "reflection_doc":
            return ("reflection_doc", "synthesis_doc")
        if role == "reflection_lens_doc":
            return REFLECTION_LENS_DOC_ROLES
        if role == "project_graph":
            return PROJECT_GRAPH_ROLES
        return (role,)

    def _latest_review_request(
        self,
        *,
        conn,
        synthesis_id: str,
        role: str,
        target_snapshot_id: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT id, status, expires_at
            FROM review_requests
            WHERE target_type = 'synthesis' AND target_id = ? AND role = ?
              AND target_snapshot_id = ?
            ORDER BY created_seq DESC
            LIMIT 1
            """,
            (synthesis_id, role, target_snapshot_id),
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
            "project_graph": "Project graph present and valid",
            "reflection_doc": "Reflection document present and valid",
            "change_spec": "Change spec present and materializable",
            "reflection_lens_doc": "Per-lens reflections submitted",
        }
        return labels.get(role, f"{role} resource present")

    def _gate_review_label(self, *, role: str) -> str:
        labels = {"reflection_reviewer": "Reflection review passed"}
        return labels.get(role, f"{role} review passed")

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
        active_count = len(
            self._non_terminal_experiments(conn=conn, project_id=project_id)
        )
        if active_count + len(experiments) > ACTIVE_EXPERIMENT_CAP:
            problems.append(
                active_experiment_cap_would_exceed_message(
                    active_count=active_count,
                    proposed_count=len(experiments),
                )
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
        key_to_claim_id: dict[str, str] = {}
        for change in changes:
            op = str(change["op"])
            key = str(change.get("key") or "").strip()
            if op == "create":
                claim_id = self.claims.create_from_synthesis(
                    conn=conn,
                    project_id=project_id,
                    synthesis_id=synthesis_id,
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
                self.claims.update_from_synthesis(
                    conn=conn,
                    project_id=project_id,
                    synthesis_id=synthesis_id,
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
        self.project_writer.stop_from_synthesis(
            conn=conn,
            project_id=project_id,
            synthesis_id=synthesis_id,
            rationale=rationale,
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
                for ref in self._claim_refs(proposal)
            ]
            proposal_key = str(proposal.get("key") or "").strip()
            experiment_id = self.experiment_writer.create_from_synthesis(
                conn=conn,
                project_id=project_id,
                synthesis_id=synthesis_id,
                name=name,
                intent=intent,
                claim_ids=claim_ids,
                proposal_key=proposal_key,
                parallelism=str(proposal.get("parallelism") or ""),
            )
            conn.execute(
                """
                INSERT INTO synthesis_experiments
                  (synthesis_id, experiment_id, proposal_key, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (synthesis_id, experiment_id, proposal_key, now_iso()),
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
        return review_snapshot_id(target_type="synthesis", target=synthesis)

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

            covered_ids = covered_terminal_ids(
                None if published is None else (published.get("corpus") or {})
            )
            if published is None:
                snapshot_claims: dict[str, str] = {}
            else:
                corpus = published.get("corpus") or {}
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
