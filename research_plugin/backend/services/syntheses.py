"""Project synthesis (reflection wave) state service.

A synthesis is the project-level counterpart of an experiment: a gated record
whose artifacts are the living project logic graph (role 'graph', the current
"logic state" of the whole project, ≤16 nodes) and the what's-next proposals
file (role 'proposals'), produced by reconciling a roster of differentiated
per-lens reflections (role 'reflection'). Gates check envelopes only; the
story's honesty is the synthesis reviewer's call, and what the graph says is
the agent's design.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..state.blobs import BlobStore
from ..state.store import StateStore, next_created_seq, row_to_dict, rows_to_dicts
from ..utils import NotFoundError, ValidationError, WorkflowError, new_id, now_iso
from .graph_lint import graph_problems
from .pinned import pinned_text_for_version, resubmit_hint
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

# Staleness threshold (advisory only): nudge a reflection once this many
# experiments have reached a terminal state since the last published
# synthesis (or since the project began, if none was ever published), or as
# soon as any claim flips to contradicted.
STALE_NEW_TERMINAL_THRESHOLD = 3


class SynthesisService:
    def __init__(self, *, store: StateStore, blobs: BlobStore | None = None) -> None:
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
            if res.get("association_role") != "reflection" or res.get("missing"):
                continue
            path = str(res.get("path") or "")
            name = path.rsplit("/", 1)[-1]
            stem = name.rsplit(".", 1)[0] if "." in name else name
            stems.setdefault(
                stem,
                {"path": path, "version_id": res.get("association_version_id")},
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
                f"synthesis is {status!r}; no transitions are allowed from a "
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
        row = conn.execute(
            """
            SELECT 1
            FROM resource_associations
            WHERE target_type = 'synthesis' AND target_id = ? AND role = ?
              AND attempt_index = (SELECT attempt_index FROM syntheses WHERE id = ?)
            LIMIT 1
            """,
            (synthesis_id, role, synthesis_id),
        ).fetchone()
        return row is not None

    def _run_validator(self, *, conn, synthesis: dict[str, Any], name: str) -> None:
        if name == "roster":
            self._validate_roster_coverage(conn=conn, synthesis=synthesis)
        elif name == "graph":
            self._validate_project_graph(conn=conn, synthesis=synthesis)
        elif name == "prose":
            self._validate_proposals(conn=conn, synthesis=synthesis)

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
                "(role 'reflection') for the current attempt, in a file named "
                "<lens_id>.md, submitted by its own subagent"
            )
        for lens in coverage["lenses"]:
            text = self._pinned_text(
                conn=conn,
                version_id=lens.get("version_id"),
                path=str(lens["path"]),
                role="reflection",
                what=f"reflection {lens['lens_id']!r}",
            )
            if not text.strip():
                raise WorkflowError(
                    f"reflection for lens {lens['lens_id']!r} ({lens['path']}) is "
                    "empty — write it and re-associate to submit the content"
                )

    def _validate_project_graph(self, *, conn, synthesis: dict[str, Any]) -> None:
        row = self._current_role_row(conn=conn, synthesis_id=synthesis["id"], role="graph")
        if row is None:
            raise WorkflowError(
                "a project logic graph resource must be submitted before synthesis_review"
            )
        text = self._pinned_text(
            conn=conn,
            version_id=row["version_id"],
            path=str(row["path"]),
            role="graph",
            what="project logic graph",
        )
        problems = graph_problems(text)
        if problems:
            raise WorkflowError(
                "project logic graph is not ready for synthesis review: "
                + "; ".join(problems)
                + ". Fix the file and re-associate it to submit the revision — "
                "see skills/research-workflow/graph-template.md."
            )

    def _validate_proposals(self, *, conn, synthesis: dict[str, Any]) -> None:
        row = self._current_role_row(
            conn=conn, synthesis_id=synthesis["id"], role="proposals"
        )
        if row is None:
            raise WorkflowError(
                "a what's-next proposals resource must be submitted before synthesis_review"
            )
        text = self._pinned_text(
            conn=conn,
            version_id=row["version_id"],
            path=str(row["path"]),
            role="proposals",
            what="proposals file",
        )
        if not text.strip():
            raise WorkflowError(
                f"proposals file {row['path']!r} is empty — write it and "
                "re-associate to submit the content"
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

    def _current_graph_version_id(self, *, conn, synthesis: dict[str, Any]) -> str | None:
        row = self._current_role_row(conn=conn, synthesis_id=synthesis["id"], role="graph")
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
        """Rejection back to synthesis only: the reflections stand, so the
        attempt is NOT bumped — the orchestrator revises the project graph
        and/or proposals and resubmits."""
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
                f"synthesis is {row['status']!r}; only a synthesis under "
                "synthesis_review can be sent back"
            )
        return row

    # ---- staleness (advisory) ----

    def reflection_signal(self, *, project_id: str, conn=None) -> dict[str, Any]:
        """How far project state has drifted from the last published synthesis.

        Computed on read, never stored. The output backs the soft 'Consider
        running a project reflection' nudge and the Home coverage badge —
        always advisory: whether new developments change the project's logic
        state is the agent's editorial call.
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
            stale = open_wave is None and (
                len(new_terminal) >= STALE_NEW_TERMINAL_THRESHOLD or contradicted_flip
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
        if published is None:
            return (
                "Consider running the project's first reflection — "
                f"{signal['terminal_experiments']} experiments have finished and "
                "no project synthesis exists yet. Use the research-reflection "
                "skill (synthesis.create) when you judge the time is right."
            )
        pieces = [
            "Consider running a project reflection — "
            f"{signal['new_terminal_since_publish']} experiments have finished "
            "since the last published synthesis"
        ]
        if signal["claims_changed_since_publish"]:
            changed = f"{signal['claims_changed_since_publish']} claims have changed"
            if signal["contradicted_flip"]:
                changed += " (including a claim now contradicted)"
            pieces.append(changed)
        pieces.append(
            "the current synthesis covers "
            f"{signal['covered_terminal_experiments']} of "
            f"{signal['terminal_experiments']} finished experiments"
        )
        return (
            "; ".join(pieces)
            + ". Whether these developments change the project's logic state is "
            "your call (research-reflection skill, synthesis.create)."
        )
