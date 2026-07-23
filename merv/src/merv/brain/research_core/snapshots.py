"""Bulk Research read model for application workflows and dashboards."""

from __future__ import annotations

from typing import Any

from .domain.reflection_policy import reflection_signal_state
from .domain.workflow_gates import TERMINAL_STATUSES
from .experiments import ExperimentService
from .facade import LiteratureSignal, ResearchSnapshot
from .gate_evaluation import GateEvaluation
from .reflections import ReflectionService
from ..kernel.state.store import BaseStateStore, row_to_dict, rows_to_dicts


class ResearchSnapshotReader:
    """Read one transaction; hydrate every requested experiment at most once."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        experiments: ExperimentService,
        reflections: ReflectionService,
    ) -> None:
        self.store = store
        self.experiments = experiments
        self.reflections = reflections

    def read(
        self,
        *,
        project_id: str | None = None,
        experiment_id: str | None = None,
        hydrate_all_experiments: bool = False,
        hydrate_selected_experiment: bool = True,
        dashboard_facts: bool = False,
    ) -> ResearchSnapshot:
        with self.store.transaction() as conn:
            project_id = self.store.require_project_id(conn=conn, project_id=project_id)
            project = (
                row_to_dict(
                    row=conn.execute(
                        "SELECT * FROM projects WHERE id = ?", (project_id,)
                    ).fetchone()
                )
                or {}
            )
            claims = rows_to_dicts(
                rows=conn.execute(
                    "SELECT id, statement, scope, status, confidence, created_at FROM claims WHERE project_id = ? ORDER BY created_at, id",
                    (project_id,),
                ).fetchall()
            )
            experiment_rows = rows_to_dicts(
                rows=conn.execute(
                    "SELECT id, name, intent, status, attempt_index, created_at, updated_at FROM experiments WHERE project_id = ? ORDER BY created_at, id",
                    (project_id,),
                ).fetchall()
            )
            selected_id = experiment_id or (
                str(experiment_rows[-1]["id"]) if experiment_rows else None
            )
            if hydrate_all_experiments:
                evaluated_states = self.experiments.list_states_with_gates(
                    conn=conn, project_id=project_id
                )
            elif selected_id and hydrate_selected_experiment:
                evaluated_states = [
                    self.experiments.get_state_with_gate(
                        experiment_id=selected_id,
                        project_id=project_id,
                        conn=conn,
                    )
                ]
            else:
                evaluated_states = []
            states = [state for state, _ in evaluated_states]
            gate_evaluations = {
                str(state["id"]): evaluation for state, evaluation in evaluated_states
            }
            selected = next(
                (state for state in states if state["id"] == selected_id), None
            )
            open_reflection, open_gate = self._reflection(
                conn=conn, project_id=project_id, terminal=False
            )
            published, published_gate = self._reflection(
                conn=conn, project_id=project_id, terminal=True
            )
            for reflection, evaluation in (
                (open_reflection, open_gate),
                (published, published_gate),
            ):
                if reflection is not None and evaluation is not None:
                    gate_evaluations[str(reflection["id"])] = evaluation
            signal = reflection_signal_state(
                current_terminal={
                    str(row["id"]): str(row["status"])
                    for row in experiment_rows
                    if str(row["status"]) in TERMINAL_STATUSES
                },
                current_claims={
                    str(claim["id"]): str(claim["status"]) for claim in claims
                },
                published=published,
                open_wave=open_reflection,
            )
            recent_claims, claim_events = (
                self._dashboard_facts(
                    conn=conn, project_id=project_id, published=published
                )
                if dashboard_facts
                else ([], [])
            )
            literature_signal = self._literature_signal(
                conn=conn, project_id=project_id
            )
            return ResearchSnapshot(
                project_id=project_id,
                requested_experiment_id=experiment_id,
                project=project,
                claims=claims,
                experiments=experiment_rows,
                experiment_states=states,
                selected_experiment=selected,
                open_reflection=open_reflection,
                latest_published_reflection=published,
                reflection_signal=signal,
                gate_evaluations=gate_evaluations,
                recent_claims=recent_claims,
                claim_events_since_reflection=claim_events,
                literature_signal=literature_signal,
            )

    def _literature_signal(self, *, conn, project_id: str) -> LiteratureSignal:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM papers WHERE project_id = ?", (project_id,)
        ).fetchone()
        unreviewed = conn.execute(
            """
            SELECT COUNT(*) AS n FROM papers p
            WHERE p.project_id = ?
              AND EXISTS (
                SELECT 1 FROM paper_links l
                WHERE l.paper_id = p.id AND l.target_type IN ('experiment', 'claim')
              )
              AND NOT EXISTS (
                SELECT 1 FROM paper_links l
                WHERE l.paper_id = p.id AND l.target_type = 'litreview_section'
              )
            """,
            (project_id,),
        ).fetchone()
        return {
            "papers_total": int(total["n"]),
            "papers_unreviewed": int(unreviewed["n"]),
        }

    def _reflection(
        self, *, conn, project_id: str, terminal: bool
    ) -> tuple[dict[str, Any] | None, GateEvaluation | None]:
        predicate = (
            "status = 'published'"
            if terminal
            else "status NOT IN ('published', 'abandoned')"
        )
        order = (
            "published_at DESC, created_seq DESC" if terminal else "created_seq DESC"
        )
        row = conn.execute(
            f"SELECT id FROM reflections WHERE project_id = ? AND {predicate} ORDER BY {order} LIMIT 1",
            (project_id,),
        ).fetchone()
        return (
            (None, None)
            if row is None
            else self.reflections.get_state_with_gate(
                reflection_id=row["id"], conn=conn
            )
        )

    def _dashboard_facts(
        self, *, conn, project_id: str, published: dict[str, Any] | None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        recent = rows_to_dicts(
            rows=conn.execute(
                "SELECT c.id, c.statement, c.status, c.confidence FROM claims c "
                "LEFT JOIN events e ON e.project_id = c.project_id AND e.target_type = 'claim' AND e.target_id = c.id "
                "AND e.type IN ('claim.created', 'claim.updated') WHERE c.project_id = ? GROUP BY c.id "
                "ORDER BY COALESCE(MAX(e.created_at), c.created_at) DESC, c.created_at DESC LIMIT 5",
                (project_id,),
            ).fetchall()
        )
        if published is None:
            return recent, []
        event = conn.execute(
            "SELECT id FROM events WHERE project_id = ? "
            "AND type = 'reflection.transitioned' AND target_type = 'reflection' "
            "AND target_id = ? ORDER BY id DESC LIMIT 1",
            (project_id, published.get("id")),
        ).fetchone()
        if event is not None:
            where, marker = "id > ?", event["id"]
        elif published.get("published_at"):
            where, marker = "created_at >= ?", published["published_at"]
        else:
            return recent, []
        events = rows_to_dicts(
            rows=conn.execute(
                "SELECT id, type, target_id, payload_json, created_at FROM events WHERE project_id = ? AND target_type = 'claim' "
                f"AND type IN ('claim.created', 'claim.updated') AND {where} ORDER BY id",
                (project_id, marker),
            ).fetchall()
        )
        return recent, events


__all__ = ["ResearchSnapshotReader"]
