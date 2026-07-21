"""HTTP presentation helpers for the Merv API."""

from __future__ import annotations

import mimetypes
from typing import Any

from ....kernel.state.activity import effective_source, is_event_ok
from ....kernel.utils import (
    ContentUnavailableError,
    NotFoundError,
)


def _activity_event_project_id(event: dict[str, Any]) -> str | None:
    value = event.get("project_id")
    if value:
        return str(value)
    args = event.get("args")
    if isinstance(args, dict) and args.get("project_id"):
        return str(args["project_id"])
    return None


def _activity_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    source_counts: dict[str, int] = {}
    event_counts: dict[str, int] = {}
    status_counts = {"ok": 0, "error": 0}
    for event in events:
        source = effective_source(event=event)
        source_counts[source] = source_counts.get(source, 0) + 1
        event_type = event.get("event") or "unknown"
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        status_counts["ok" if is_event_ok(event=event) else "error"] += 1
    return {
        "total": len(events),
        "count": len(events),
        "source_counts": source_counts,
        "event_counts": event_counts,
        "status_counts": status_counts,
        "window": len(events),
    }


class ResearchHttpApi:
    """HTTP view helpers over ControlApp. Domain logic stays in services."""

    _LOCAL_DATA_PLANE_RESPONSE_KEYS = frozenset(
        {"repo_root", "local_sync_dir", "local_experiment_dir"}
    )

    def __init__(self, *, app: Any) -> None:
        self.app = app

    def _present(self, value: Any) -> Any:
        return self._strip_local_data_plane(value)

    def experiment_state_view(
        self, *, project_id: str, experiment_id: str
    ) -> dict[str, Any]:
        detail = self.app.experiment_detail_query(
            project_id=project_id,
            experiment_id=experiment_id,
        )
        return self._present(detail)

    @classmethod
    def _strip_local_data_plane(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._strip_local_data_plane(item)
                for key, item in value.items()
                if key not in cls._LOCAL_DATA_PLANE_RESPONSE_KEYS
            }
        if isinstance(value, list):
            return [cls._strip_local_data_plane(item) for item in value]
        return value

    def activity(
        self,
        limit: int,
        source: str | None = None,
        project_id: str | None = None,
        project_ids: set[str] | None = None,
        include_unscoped_events: bool = True,
    ) -> dict[str, Any]:
        event_filter = None
        if project_ids is not None:
            allowed = {str(pid) for pid in project_ids if str(pid)}

            def _allowed(ev: dict[str, Any]) -> bool:
                pid = _activity_event_project_id(ev)
                if project_id is not None:
                    return pid == project_id
                return (pid in allowed) or (include_unscoped_events and pid is None)

            event_filter = _allowed
        result = self.app.activity.recent(
            limit=limit, source=source, event_filter=event_filter
        )
        events = result["events"]
        if project_id is not None and project_ids is None:
            # Single-app mode serves one repo (one project), so this only drops
            # the rare cross-project tool.call carrying a different project_id in
            # its arguments. Events with no project attribution (e.g.
            # http.request) belong to this app and are kept.
            def _belongs(ev: dict[str, Any]) -> bool:
                pid = _activity_event_project_id(ev)
                return pid in (None, project_id)

            events = [ev for ev in events if _belongs(ev)]
        payload = {
            "filter": {
                key: value
                for key, value in (("source", source), ("project_id", project_id))
                if value
            },
            "events": events,
            "summary": (
                # Summarize over the full filtered scan window, not the trimmed
                # display slice, so a tenant's counts reflect everything scanned
                # (up to `window`) rather than capping at `limit`.
                _activity_summary(result.get("scanned_filtered", events))
                if project_ids is not None or not include_unscoped_events
                else result["summary"]
            ),
        }
        return self._present(payload)

    def tool_call_stats(
        self,
        *,
        minutes: int | None,
        source: str | None,
        status: str | None,
        tool: str | None,
        project_id: str | None,
        project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
        limit: int,
        sort: str,
        order: str,
    ) -> dict[str, Any]:
        return self.app.tool_calls.stats(
            minutes=minutes,
            source=source,
            status=status,
            tool=tool,
            project_id=project_id,
            project_ids=project_ids,
            limit=limit,
            sort=sort,
            order=order,
        )

    def tool_call_detail(
        self,
        call_id: int,
        *,
        project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        record = self.app.tool_calls.get(call_id=call_id, project_ids=project_ids)
        if record is None:
            raise NotFoundError(f"tool call not found: {call_id}")
        return self._present(record)

    def experiments_view(self, project_id: str) -> dict[str, Any]:
        # Full per-experiment state for the UI; the experiment.list tool stays slim.
        experiments = self.app.experiment_collection_query.rich(
            project_id=project_id
        )
        return {
            "experiments": [
                self._experiment_view_model(exp=exp) for exp in experiments
            ],
            "current": experiments[-1] if experiments else None,
            "resource_use": [],
            "recent_runs": [],
        }

    def resources_tree(self, project_id: str) -> dict[str, Any]:
        resources = self.app.resources.list_resources(project_id=project_id)[
            "resources"
        ]
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for resource in resources:
            by_kind.setdefault(resource.get("kind", "other"), []).append(resource)
        return {"resources": resources, "tree": by_kind}

    def resource_content(
        self, project_id: str, resource_id: str, version: str | None = None
    ) -> dict[str, Any]:
        return self.app.hosted_resource_content_query(
            project_id=project_id,
            resource_id=resource_id,
            version_id=version,
        )

    def resource_file(
        self, project_id: str, resource_id: str, rel: str | None = None
    ) -> tuple[bytes, dict[str, str]]:
        resource = self.app.resources.resolve(
            project_id=project_id, resource_id=resource_id
        )
        if rel:
            blob = self.app.artifacts.submitted_figure(resource=resource, link_path=rel)
            if blob is not None:
                data, name = blob
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                return data, {
                    "Content-Type": mime,
                    "Content-Disposition": f'inline; filename="{name}"',
                }
            raise ContentUnavailableError(
                "figure bytes are unavailable in this mode",
                details={"rel": rel, "reason": "content_unavailable_in_this_mode"},
            )
        raise ContentUnavailableError(
            "file bytes are unavailable in this mode",
            details={"reason": "content_unavailable_in_this_mode"},
        )

    def filter_experiments(self, project_id: str, status: str | None) -> dict[str, Any]:
        # Full per-experiment state for the UI; the experiment.list tool stays slim.
        experiments = self.app.experiment_collection_query.rich(
            project_id=project_id
        )
        if status:
            experiments = [exp for exp in experiments if exp.get("status") == status]
        return {"experiments": experiments}

    def filter_resources(self, project_id: str, kind: str | None) -> dict[str, Any]:
        resources = self.app.resources.list_resources(project_id=project_id)[
            "resources"
        ]
        if kind:
            resources = [res for res in resources if res.get("kind") == kind]
        return {"resources": resources}

    # ---- sandbox UI projections ----
    # The domain SandboxService returns raw rows / sampled data; the UI-facing
    # shaping lives here so presentation stays out of the service.

    def sandbox_get_view(
        self,
        *,
        project_id: str,
        experiment_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        row = self.app.sandboxes.get_row(
            experiment_id=experiment_id, project_id=project_id, sandbox_uid=sandbox_uid
        )
        if row is None:
            return {
                "experiment_id": experiment_id or "",
                "sandbox_uid": sandbox_uid or "",
                "status": "none",
                "sandbox": None,
            }
        return self._present(self.app.sandboxes.row_view(row=row))

    def sandbox_list_view(self, *, project_id: str) -> dict[str, Any]:
        return self._present(
            {
                "sandboxes": [
                    self.app.sandboxes.row_view(row=row)
                    for row in self.app.sandboxes.rows(project_id=project_id)
                ]
            }
        )

    def compute_cost_view(self, *, project_id: str) -> dict[str, Any]:
        return self._present(self.app.compute_cost_query(project_id=project_id))

    def sandbox_metrics_view(
        self,
        *,
        project_id: str,
        experiment_id: str | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        return self.app.sandboxes.sample_metrics(
            experiment_id=experiment_id or "",
            project_id=project_id,
            sandbox_uid=sandbox_uid,
        )

    def experiment_logic_graph(
        self, *, project_id: str, experiment_id: str
    ) -> dict[str, Any]:
        return self.app.logic_graph_query.experiment(
            project_id=project_id, experiment_id=experiment_id
        )

    def reflections_view(self, *, project_id: str) -> dict[str, Any]:
        return self.app.logic_graph_query.reflections(project_id=project_id)

    def reflection_detail(
        self, *, project_id: str, reflection_id: str
    ) -> dict[str, Any]:
        return self.app.logic_graph_query.reflection(
            reflection_id=reflection_id, project_id=project_id
        )

    def project_logic_graph(self, *, project_id: str) -> dict[str, Any]:
        return self.app.logic_graph_query.project(project_id=project_id)

    def reflection_graph(
        self, *, project_id: str, reflection_id: str
    ) -> dict[str, Any]:
        return self.app.logic_graph_query.reflection_graph(
            project_id=project_id, reflection_id=reflection_id
        )

    def _experiment_view_model(self, *, exp: dict[str, Any]) -> dict[str, Any]:
        current = exp.get("current_attempt_resources", [])
        plans = [res["id"] for res in current if res.get("association_role") == "plan"]
        results = [
            res["id"] for res in current if res.get("association_role") == "result"
        ]
        return {
            **exp,
            "tests_claims": [claim["id"] for claim in exp.get("tested_claims", [])],
            "input_resources": [
                res["id"]
                for res in current
                if res.get("association_role") in {"input", "code", "config", "plan"}
            ],
            "output_resources": results,
            "check": {
                "summary": exp.get("intent", ""),
                "claims": [claim["id"] for claim in exp.get("tested_claims", [])],
                "success_criteria": "",
                "metrics": [],
            },
            "did": {
                "summary": exp.get("revision_context", ""),
                "runs": [],
                "input_resources": plans,
                "output_resources": results,
                "last_run_at": exp.get("updated_at"),
            },
            "learned": {
                "summary": "",
                "is_concluded": exp.get("status") == "complete",
                "completion_blockers": [],
                "headline_metrics": {},
                "headline_metric_details": [],
            },
        }
