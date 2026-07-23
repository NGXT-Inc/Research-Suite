"""Pure HTTP response shaping over already-selected public operations."""

from __future__ import annotations

from typing import Any

from ....kernel.state.activity import effective_source, is_event_ok
from ....kernel.utils import NotFoundError
from ....sandbox.facade import SandboxFacade
from .dependencies import ActivityTelemetry, ToolCallTelemetry

_LOCAL_DATA_PLANE_RESPONSE_KEYS = frozenset(
    {"repo_root", "local_sync_dir", "local_experiment_dir"}
)


def present(value: Any) -> Any:
    """Remove machine-local implementation details from public responses."""
    if isinstance(value, dict):
        return {
            key: present(item)
            for key, item in value.items()
            if key not in _LOCAL_DATA_PLANE_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [present(item) for item in value]
    return value


def _event_project_id(event: dict[str, Any]) -> str | None:
    value = event.get("project_id")
    if value:
        return str(value)
    args = event.get("args")
    return str(args["project_id"]) if isinstance(args, dict) and args.get("project_id") else None


def _activity_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    sources: dict[str, int] = {}
    types: dict[str, int] = {}
    statuses = {"ok": 0, "error": 0}
    for event in events:
        source = effective_source(event=event)
        event_type = event.get("event") or "unknown"
        sources[source] = sources.get(source, 0) + 1
        types[event_type] = types.get(event_type, 0) + 1
        statuses["ok" if is_event_ok(event=event) else "error"] += 1
    return {
        "total": len(events),
        "count": len(events),
        "source_counts": sources,
        "event_counts": types,
        "status_counts": statuses,
        "window": len(events),
    }


def activity_view(
    activity: ActivityTelemetry,
    *,
    limit: int,
    source: str | None = None,
    project_id: str | None = None,
    project_ids: set[str] | None = None,
    include_unscoped_events: bool = True,
) -> dict[str, Any]:
    event_filter = None
    if project_ids is not None:
        allowed = {str(pid) for pid in project_ids if str(pid)}

        def event_filter(event: dict[str, Any]) -> bool:
            pid = _event_project_id(event)
            return pid == project_id if project_id else (
                pid in allowed or (include_unscoped_events and pid is None)
            )

    result = activity.recent(limit=limit, source=source, event_filter=event_filter)
    events = result["events"]
    if project_id is not None and project_ids is None:
        events = [event for event in events if _event_project_id(event) in (None, project_id)]
    summary = (
        _activity_summary(result.get("scanned_filtered", events))
        if project_ids is not None or not include_unscoped_events
        else result["summary"]
    )
    return present(
        {
            "filter": {
                key: value
                for key, value in (("source", source), ("project_id", project_id))
                if value
            },
            "events": events,
            "summary": summary,
        }
    )


def tool_call_detail(
    telemetry: ToolCallTelemetry,
    call_id: int,
    *,
    project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    record = telemetry.get(call_id=call_id, project_ids=project_ids)
    if record is None:
        raise NotFoundError(f"tool call not found: {call_id}")
    return present(record)


def experiment_view_model(exp: dict[str, Any]) -> dict[str, Any]:
    current = exp.get("current_attempt_artifacts", [])
    plans = [res["id"] for res in current if res.get("role") == "plan"]
    results = [res["id"] for res in current if res.get("role") == "result"]
    claims = [claim["id"] for claim in exp.get("tested_claims", [])]
    return {
        **exp,
        "tests_claims": claims,
        "input_artifacts": [
            res["id"]
            for res in current
            if res.get("role") in {"input", "code", "config", "plan"}
        ],
        "output_artifacts": results,
        "check": {
            "summary": exp.get("intent", ""),
            "claims": claims,
            "success_criteria": "",
            "metrics": [],
        },
        "did": {
            "summary": exp.get("revision_context", ""),
            "runs": [],
            "input_artifacts": plans,
            "output_artifacts": results,
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


def experiments_view(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "experiments": [experiment_view_model(exp) for exp in experiments],
        "current": experiments[-1] if experiments else None,
        "artifact_use": [],
        "recent_runs": [],
    }


def sandbox_view(
    sandboxes: SandboxFacade,
    *,
    project_id: str,
    experiment_id: str | None = None,
    sandbox_uid: str | None = None,
) -> dict[str, Any]:
    row = sandboxes.get_row(
        experiment_id=experiment_id, project_id=project_id, sandbox_uid=sandbox_uid
    )
    if row is None:
        return {
            "experiment_id": experiment_id or "",
            "sandbox_uid": sandbox_uid or "",
            "status": "none",
            "sandbox": None,
        }
    return present(sandboxes.row_view(row=row))


def sandbox_list_view(sandboxes: SandboxFacade, *, project_id: str) -> dict[str, Any]:
    return present(
        {"sandboxes": [sandboxes.row_view(row=row) for row in sandboxes.rows(project_id=project_id)]}
    )
