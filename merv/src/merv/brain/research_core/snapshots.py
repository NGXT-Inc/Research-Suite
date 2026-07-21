"""Bulk Research read model for application workflows and dashboards."""

from __future__ import annotations

from typing import Any

from .domain.reflection_gates import REFLECTION_GATE_TABLE
from .domain.reflection_policy import reflection_signal_state
from .domain.workflow_gates import GATE_TABLE, TERMINAL_STATUSES
from .experiments import ExperimentService
from .facade import ResearchSnapshot, ReviewGateSnapshot
from .reflections import ReflectionService
from .reviews import ReviewService
from ..kernel.state.store import BaseStateStore, row_to_dict, rows_to_dicts


class ResearchSnapshotReader:
    """Read one transaction; hydrate every requested experiment at most once."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        experiments: ExperimentService,
        reflections: ReflectionService,
        reviews: ReviewService,
    ) -> None:
        self.store = store
        self.experiments = experiments
        self.reflections = reflections
        self.reviews = reviews

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
            state_ids = (
                [str(row["id"]) for row in experiment_rows]
                if hydrate_all_experiments
                else (
                    [selected_id] if selected_id and hydrate_selected_experiment else []
                )
            )
            states = [
                self.experiments.get_state(
                    experiment_id=state_id, project_id=project_id, conn=conn
                )
                for state_id in state_ids
            ]
            selected = next(
                (state for state in states if state["id"] == selected_id), None
            )
            open_reflection = self._reflection(
                conn=conn, project_id=project_id, terminal=False
            )
            published = self._reflection(
                conn=conn, project_id=project_id, terminal=True
            )
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
            review_gates = self._review_gates(
                conn=conn, experiments=states, reflection=open_reflection
            )
            recent_claims, claim_events = (
                self._dashboard_facts(
                    conn=conn, project_id=project_id, published=published
                )
                if dashboard_facts
                else ([], [])
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
                review_gates=review_gates,
                recent_claims=recent_claims,
                claim_events_since_reflection=claim_events,
            )

    def _reflection(
        self, *, conn, project_id: str, terminal: bool
    ) -> dict[str, Any] | None:
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
            None
            if row is None
            else self.reflections.get_state(reflection_id=row["id"], conn=conn)
        )

    def _review_gates(
        self,
        *,
        conn,
        experiments: list[dict[str, Any]],
        reflection: dict[str, Any] | None,
    ) -> dict[tuple[str, str, str], ReviewGateSnapshot]:
        subjects = [("experiment", item, GATE_TABLE) for item in experiments]
        if reflection is not None:
            subjects.append(("reflection", reflection, REFLECTION_GATE_TABLE))
        result: dict[tuple[str, str, str], ReviewGateSnapshot] = {}
        for target_type, target, table in subjects:
            forward = table.get(str(target.get("status") or ""))
            if forward is None or forward.review is None:
                continue
            role = forward.review.role
            target_id = str(target["id"])
            gate = self.reviews.gate_state(
                conn=conn, target_type=target_type, target_id=target_id, role=role
            )
            result[(target_type, target_id, role)] = ReviewGateSnapshot(
                satisfied=bool(gate["satisfied"]),
                blocked_reason=str(gate.get("blocked_reason") or ""),
                request=self.reviews.open_request(
                    conn=conn, target_type=target_type, target_id=target_id, role=role
                ),
            )
        return result

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
