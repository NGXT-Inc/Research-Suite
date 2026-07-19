"""Project orientation view for the agent-facing project tool (action=current)."""

from __future__ import annotations

from contextlib import closing
import json
from typing import Any

from merv.shared.artifact_roles import PROJECT_GRAPH_ROLES

from .domain.reflection_policy import covered_terminal_ids
from .domain.vocabulary import EXPERIMENT_TERMINAL_STATUSES
from .projects import ProjectService
from .reflections import ReflectionService
from ..kernel.state.store import BaseStateStore, rows_to_dicts


class ProjectOverviewService:
    """Builds the compact project orientation block for agents."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        projects: ProjectService,
        reflections: ReflectionService,
    ) -> None:
        self.store = store
        self.projects = projects
        self.reflections = reflections

    def current_project(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        """Project identity plus the small orientation block every agent sees."""
        current = self.projects.current(tenant_id=tenant_id)
        if not current.get("exists"):
            return current
        project = current.get("project") or {}
        project_id = str(project.get("id") or "")
        if not project_id:
            return current
        return {
            **current,
            "at_a_glance": self._project_at_a_glance(project_id=project_id),
        }

    def _project_at_a_glance(self, *, project_id: str) -> dict[str, Any]:
        with closing(self.store.connect()) as conn:
            latest = self.reflections.latest_published(conn=conn, project_id=project_id)
            open_wave = self.reflections.open_reflection(
                conn=conn, project_id=project_id
            )
            experiments = rows_to_dicts(
                rows=conn.execute(
                    """
                    SELECT id, name, intent, status, attempt_index, created_at, updated_at
                    FROM experiments
                    WHERE project_id = ?
                    ORDER BY created_at, id
                    """,
                    (project_id,),
                ).fetchall()
            )
            recent_claims = rows_to_dicts(
                rows=conn.execute(
                    """
                    SELECT c.id, c.statement, c.status, c.confidence
                    FROM claims c
                    LEFT JOIN events e
                      ON e.project_id = c.project_id
                     AND e.target_type = 'claim'
                     AND e.target_id = c.id
                     AND e.type IN ('claim.created', 'claim.updated')
                    WHERE c.project_id = ?
                    GROUP BY c.id
                    ORDER BY COALESCE(MAX(e.created_at), c.created_at) DESC, c.created_at DESC
                    LIMIT 5
                    """,
                    (project_id,),
                ).fetchall()
            )
            claim_events: list[dict[str, Any]] = []
            if latest is not None:
                publish_event = conn.execute(
                    """
                    SELECT id FROM events
                    WHERE project_id = ? AND type = 'reflection.transitioned'
                      AND target_type = 'reflection' AND target_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (project_id, latest.get("id")),
                ).fetchone()
                if publish_event is not None:
                    claim_events = rows_to_dicts(
                        rows=conn.execute(
                            """
                            SELECT id, type, target_id, payload_json, created_at
                            FROM events
                            WHERE project_id = ? AND target_type = 'claim'
                              AND type IN ('claim.created', 'claim.updated')
                              AND id > ?
                            ORDER BY id
                            """,
                            (project_id, publish_event["id"]),
                        ).fetchall()
                    )
                elif latest.get("published_at"):
                    claim_events = rows_to_dicts(
                        rows=conn.execute(
                            """
                            SELECT id, type, target_id, payload_json, created_at
                            FROM events
                            WHERE project_id = ? AND target_type = 'claim'
                              AND type IN ('claim.created', 'claim.updated')
                              AND created_at >= ?
                            ORDER BY id
                            """,
                            (project_id, latest.get("published_at")),
                        ).fetchall()
                    )

        terminal_statuses = set(EXPERIMENT_TERMINAL_STATUSES)
        terminal_experiments = [
            exp for exp in experiments if str(exp.get("status")) in terminal_statuses
        ]
        active_experiments = [
            exp
            for exp in experiments
            if str(exp.get("status")) not in terminal_statuses
        ]
        covered_ids = covered_terminal_ids((latest or {}).get("corpus"))
        experiments_since_reflection = [
            exp for exp in terminal_experiments if str(exp.get("id")) not in covered_ids
        ]
        changed_claim_ids = [
            str(event.get("target_id"))
            for event in claim_events
            if event.get("target_id")
            and self._event_payload(event).get("source_reflection_id")
            != (latest or {}).get("id")
        ]
        seen_claim_ids: set[str] = set()
        changed_claim_ids = [
            claim_id
            for claim_id in changed_claim_ids
            if not (claim_id in seen_claim_ids or seen_claim_ids.add(claim_id))
        ]

        project_reflection = None
        if latest is not None:
            graph = self._resource_link_for_role(
                reflection=latest,
                roles=PROJECT_GRAPH_ROLES,
                label="Current project graph",
                canonical_role="project_graph",
            )
            reflection_doc = self._resource_link_for_role(
                reflection=latest,
                roles=("reflection_doc", "synthesis_doc"),
                label="Latest reflection doc",
                canonical_role="reflection_doc",
            )
            project_reflection = {
                "reflection_id": latest.get("id"),
                "time": latest.get("published_at"),
                "reflection_doc_resource_id": (
                    reflection_doc.get("resource_id") if reflection_doc else None
                ),
                "project_graph_resource_id": (
                    graph.get("resource_id") if graph else None
                ),
            }

        covered_count = len(
            covered_ids & {str(exp.get("id")) for exp in terminal_experiments}
        )
        return {
            "summary": self._at_a_glance_summary(
                latest=latest,
                terminal_count=len(terminal_experiments),
                covered_count=covered_count,
                experiments_since=len(experiments_since_reflection),
                claims_changed=len(changed_claim_ids),
            ),
            "recent": {
                "experiments": [
                    {
                        "id": exp.get("id"),
                        "name": exp.get("name"),
                        "status": exp.get("status"),
                    }
                    for exp in sorted(
                        experiments,
                        key=lambda item: str(
                            item.get("updated_at") or item.get("created_at") or ""
                        ),
                        reverse=True,
                    )[:5]
                ],
                "claims": [
                    {
                        "id": claim.get("id"),
                        "status": claim.get("status"),
                        "confidence": claim.get("confidence"),
                        "statement": claim.get("statement"),
                    }
                    for claim in recent_claims
                ],
            },
            "project_reflection": project_reflection,
            "since_reflection": {
                "finished_experiment_ids": [
                    str(exp.get("id")) for exp in experiments_since_reflection
                ],
                "changed_claim_ids": changed_claim_ids,
                "active_experiment_ids": [
                    str(exp.get("id")) for exp in active_experiments
                ],
            },
            "open_reflection_id": open_wave.get("id") if open_wave else None,
        }

    def _event_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        raw = event.get("payload_json")
        if not raw:
            return {}
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _resource_link_for_role(
        self,
        *,
        reflection: dict[str, Any],
        roles: tuple[str, ...],
        label: str,
        canonical_role: str,
    ) -> dict[str, Any] | None:
        attempt = reflection.get("attempt_index")
        candidates = [
            res
            for res in reflection.get("resources", [])
            if res.get("association_role") in roles
            and res.get("association_attempt_index") == attempt
        ]
        if not candidates:
            return None
        role_rank = {role: index for index, role in enumerate(roles)}
        res = min(
            candidates,
            key=lambda item: (
                role_rank.get(str(item.get("association_role")), len(roles)),
                -(item.get("association_rowid") or 0),
            ),
        )
        return {
            "label": label,
            "kind": "resource",
            "role": canonical_role,
            "legacy_role": (
                res.get("association_role")
                if res.get("association_role") != canonical_role
                else None
            ),
            "resource_id": res.get("id"),
            "path": res.get("path"),
            "version_id": res.get("association_version_id"),
            "read_with": "resource.find",
            "read_args": {"resource_id": res.get("id"), "include_history": True},
        }

    def _at_a_glance_summary(
        self,
        *,
        latest: dict[str, Any] | None,
        terminal_count: int,
        covered_count: int,
        experiments_since: int,
        claims_changed: int,
    ) -> str:
        if latest is None:
            summary = (
                f"No published reflection; 0/{terminal_count} finished "
                f"experiments covered; {terminal_count} finished experiments since."
            )
            if terminal_count >= 3:
                summary += " New reflection recommended."
            return summary
        pieces = [
            f"Latest reflection covers {covered_count}/{terminal_count} finished experiments"
        ]
        if experiments_since:
            pieces.append(f"{experiments_since} finished experiments since")
        if claims_changed:
            pieces.append(f"{claims_changed} claims changed since")
        if len(pieces) == 1:
            pieces.append("no newer experiment or claim changes detected")
        summary = "; ".join(pieces) + "."
        if experiments_since >= 3:
            summary += " New reflection recommended."
        return summary
