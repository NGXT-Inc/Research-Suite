"""FastAPI routes for the Research Plugin backend.

Owns only the HTTP shape: route handlers, request/response shaping, error
translation, and the FastAPI app factory. The uvicorn server wrapper and
marker lifecycle live in `http_server`.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from .. import __version__
from .admin_http import register_admin_routes
from ..app import ResearchPluginApp
from .daemon_http import register_daemon_routes
from ..version import CLIENT_VERSION_HEADER, MIN_PROXY_VERSION, is_below_floor, meta
from ..tools.contracts import (
    DATA_PLANE_TOOL_NAMES,
    PROJECT_SCOPED_TOOL_NAMES,
    TOOL_CONTRACTS,
)
from .feed_http import register_feed_routes
from ..daemon.project_router import ProjectRouter
from ..domain.graph_lint import MAX_GRAPH_NODES, graph_problems
from ..domain.resource_selection import preferred_associated_resource
from ..domain.vocabulary import GATED_ROLES, PROJECT_GRAPH_ROLES
from .http_policy import (
    HOSTED_CONTROL_TOOL_POLICIES,
    HTTP_DATA_PLANE_FEATURE_TO_TOOL,
    HttpSurfacePolicy,
)
from .mcp_http import register_mcp_routes
from ..services.figure_view import build_experiment_figure
from ..services.identity import LOCAL_PRINCIPAL
from ..mlflow import mlflow_experiment_name
from ..utils import (
    ContentUnavailableError,
    DataPlaneRequiredError,
    NotFoundError,
    ResearchPluginError,
    ValidationError,
    WorkflowError,
)
from ..state import monotonic_ms
from ..state.activity import effective_source, is_event_ok


JsonBody = dict[str, Any] | None
UI_CORS_HEADERS = ["Content-Type", "Accept", "Authorization", "X-RP-Client-Version"]


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
    """HTTP view helpers over ResearchPluginApp. Domain logic stays in services."""

    _LOCAL_DATA_PLANE_RESPONSE_KEYS = frozenset(
        {"repo_root", "local_sync_dir", "local_experiment_dir"}
    )

    def __init__(
        self, *, app: ResearchPluginApp, expose_local_data_plane: bool = True
    ) -> None:
        self.app = app
        self.expose_local_data_plane = expose_local_data_plane

    def call_tool(self, *, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._present(
            self.app.call_tool(name=name, arguments=arguments, activity_source="http")
        )

    def _present(self, value: Any) -> Any:
        if self.expose_local_data_plane:
            return value
        return self._strip_local_data_plane(value)

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

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "repo_root": str(self.app.workspace.repo_root),
            "store": str(self.app.store.db_path),
            "activity_log": str(self.app.activity.log_path),
            "mlflow": self.app.mlflow_tracking.health(),
        }

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
            "filter": {key: value for key, value in (("source", source), ("project_id", project_id)) if value},
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
        if self.expose_local_data_plane:
            payload["activity_log"] = str(self.app.activity.log_path)
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

    def tool_calls_clear(
        self,
        *,
        project_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        return self.app.tool_calls.clear(project_ids=project_ids)

    def home(self, project_id: str) -> dict[str, Any]:
        # The UI needs the rich shape (project-wide claims/experiments); the
        # slimmed `workflow.status_and_next` tool is for the agent only, so call
        # the service method directly here.
        status = self.app.workflow.status_and_next(project_id=project_id)
        resources = self.call_tool(name="resource.list", arguments={"project_id": project_id})["resources"]
        reviews = self.review_queue(project_id=project_id)
        events = self.events(project_id=project_id, limit=25)["events"]
        claims = status["project"]["active_claims"]
        experiments = [
            # Full shape for the UI; the experiment.get_state tool stays slim for the agent.
            self.app.experiments.get_state(experiment_id=exp["id"], project_id=project_id)
            for exp in status["project"]["active_experiments"]
        ]
        active_work = self.app.workflow.active_work(project_id=project_id)
        active_experiments = active_work["active_experiments"]
        active_processes = active_work["active_processes"]
        active_experiment = active_experiments[0] if active_experiments else None
        workflow = active_experiment.get("workflow") if active_experiment else status["workflow"]
        return self._present({
            "project": status["project"],
            "claims": claims,
            "experiments": experiments,
            "active_experiments": active_experiments,
            "active_processes": active_processes,
            "resources": resources,
            "reviews": reviews,
            "pending_change_sets": [],
            "recent_events": events,
            "stats": {
                "claims": len(claims),
                "experiments": len(experiments),
                "active_experiments": len(active_experiments),
                "active_processes": len(active_processes),
                "resources": len(resources),
                "open_reviews": len(reviews["requests"]),
            },
            "workflow": workflow,
            "active_experiment": active_experiment,
            # Central, cross-experiment MLflow endpoint so the UI can offer a
            # project-level entry point.
            "mlflow": self.app.mlflow_tracking.health(),
        })

    def experiments_view(self, project_id: str) -> dict[str, Any]:
        # Full per-experiment state for the UI; the experiment.list tool stays slim.
        experiments = self.app.experiments.list_experiments(project_id=project_id)["experiments"]
        return {
            "experiments": [self._experiment_view_model(exp=exp) for exp in experiments],
            "current": experiments[-1] if experiments else None,
            "resource_use": [],
            "recent_runs": [],
        }

    def resources_tree(self, project_id: str) -> dict[str, Any]:
        resources = self.call_tool(name="resource.list", arguments={"project_id": project_id})["resources"]
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for resource in resources:
            by_kind.setdefault(resource.get("kind", "other"), []).append(resource)
        return {"resources": resources, "tree": by_kind}

    def review_queue(self, project_id: str) -> dict[str, Any]:
        return self.app.reviews.queue(project_id=project_id)

    def start_review(
        self,
        *,
        project_id: str,
        body: dict[str, Any],
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        self.app.reviews.assert_request_in_project(
            project_id=project_id, review_request_id=body.get("review_request_id")
        )
        return self._present(
            self.app.call_tool(
                name="review.start",
                arguments=body,
                activity_source="http",
                internal_kwargs=(
                    {"tenant_id": tenant_id} if tenant_id is not None else None
                ),
                telemetry_project_id=project_id,
            )
        )

    def submit_review(self, *, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.app.reviews.assert_session_in_project(
            project_id=project_id, review_session_id=body.get("review_session_id")
        )
        return self.call_tool(name="review.submit", arguments=body)

    def events(self, project_id: str, limit: int = 100) -> dict[str, Any]:
        return self.app.store.recent_events(project_id=project_id, limit=limit)

    def get_claim(self, project_id: str, claim_id: str) -> dict[str, Any]:
        claims = self.call_tool(name="claim.list", arguments={"project_id": project_id})["claims"]
        for claim in claims:
            if claim["id"] == claim_id:
                return claim
        raise NotFoundError(f"claim not found: {claim_id}")

    def resource_content(
        self, project_id: str, resource_id: str, version: str | None = None
    ) -> dict[str, Any]:
        resource = self.call_tool(name="resource.resolve", arguments={"project_id": project_id, "resource_id": resource_id})
        # An explicit version pins the EXACT submitted bytes of one resource
        # version (used by the reflection-wave UI to render a past wave's
        # graph, reflection doc, or change spec faithfully, not the latest
        # living file).
        if version:
            return self._resource_content_at_version(
                project_id=project_id, resource=resource, version_id=version
            )
        # Gated-role artifacts render their SUBMITTED bytes (the content the
        # gates lint and reviewers grade); other roles read the live file —
        # a local-mode convenience the cloud profile will not have.
        pinned = self._pinned_gated_text(project_id=project_id, resource=resource)
        if pinned is not None:
            text, version_id = pinned
            return {
                "resource": resource,
                "path": resource["path"],
                "content": text,
                "text": text,
                "size_bytes": len(text.encode("utf-8")),
                "source": "submitted",
                "version_id": version_id,
            }
        # Non-gated roles (e.g. result) read the live file — a local-mode
        # convenience. In control mode there is no checkout, and result files
        # stay metadata-only (fixed decision 6): return a clean, documented
        # degraded shape rather than a 500/404 so the UI can render an explicit
        # "content unavailable in this mode" state (open decision F).
        if not self.expose_local_data_plane:
            return {
                "resource": resource,
                "path": resource.get("path"),
                "content": None,
                "text": None,
                "available": False,
                "source": "unavailable",
                "reason": "content_unavailable_in_this_mode",
                "detail": (
                    "this file's bytes live only on the offline data-plane daemon; "
                    "result-role files are metadata-only in the cloud"
                ),
            }
        path = self._resource_path(resource=resource)
        text = path.read_text(errors="replace")
        return {
            "resource": resource,
            "path": resource["path"],
            "content": text,
            "text": text,
            "size_bytes": path.stat().st_size,
            "source": "live",
            "available": True,
        }

    def _resource_content_at_version(
        self, *, project_id: str, resource: dict[str, Any], version_id: str
    ) -> dict[str, Any]:
        """Serve the exact submitted bytes of one pinned resource version.

        The version must belong to this resource — its current version or any
        of its associations' versions — otherwise 404 (so a cross-resource
        version_id can't be used to read arbitrary blobs). A missing/undecodable
        blob degrades to the same {available: False} shape the control-mode and
        live paths use, so the UI renders ContentUnavailable instead of a 500."""
        valid = {
            str(a.get("version_id"))
            for a in resource.get("associations", [])
            if a.get("version_id")
        }
        current_version = resource.get("current_version_id")
        if current_version:
            valid.add(str(current_version))
        if version_id not in valid:
            raise NotFoundError(
                f"version {version_id} is not associated with resource {resource.get('id')}"
            )
        try:
            text = self.app.resources.pinned_text_for_version(
                version_id=version_id,
                what="resource content",
                role="",
            )
        except WorkflowError as exc:
            return {
                "resource": resource,
                "path": resource.get("path"),
                "content": None,
                "text": None,
                "available": False,
                "source": "unavailable",
                "reason": "version_unavailable",
                "detail": str(exc),
                "version_id": version_id,
            }
        return {
            "resource": resource,
            "path": resource.get("path"),
            "content": text,
            "text": text,
            "size_bytes": len(text.encode("utf-8")),
            "source": "submitted",
            "version_id": version_id,
            "available": True,
        }

    def _latest_gated_version_id(self, *, resource: dict[str, Any]) -> str | None:
        best: tuple[int, str] | None = None
        for assoc in resource.get("associations", []):
            role = str(assoc.get("role") or "")
            version_id = assoc.get("version_id")
            if role not in GATED_ROLES or not version_id:
                continue
            attempt = int(assoc.get("attempt_index") or 0)
            if best is None or attempt >= best[0]:
                best = (attempt, str(version_id))
        return best[1] if best else None

    def _pinned_gated_text(
        self, *, project_id: str, resource: dict[str, Any]
    ) -> tuple[str, str] | None:
        """The latest submitted bytes for a gated-role resource (decoded) plus
        the version id they came from, or None when the resource has no gated
        association (the caller then reads the live file).

        The documented no-version default is the *latest* submitted bytes, so
        this resolves to the resource's ``current_version_id`` — the newest
        observed version, whose bytes are captured into the blob store when it
        is associated as a gated artifact. That keeps a living file pinned by
        several targets (e.g. ``project/reflection.md`` associated by multiple
        reflection waves) serving its newest version rather than whichever
        association happens to carry the highest per-target attempt index. It
        falls back to the latest gated association only when the current
        version's own bytes were never submitted (a live edit observed but not
        re-associated)."""
        latest_assoc = self._latest_gated_version_id(resource=resource)
        if latest_assoc is None:
            return None
        candidates: list[str] = []
        current = resource.get("current_version_id")
        if current:
            candidates.append(str(current))
        if latest_assoc not in candidates:
            candidates.append(latest_assoc)
        for version_id in candidates:
            text = self.app.resources.submitted_text_for_version(
                version_id=version_id
            )
            if text is not None:
                return text, version_id
        return None

    def resource_file(
        self, project_id: str, resource_id: str, rel: str | None = None
    ) -> tuple[bytes, dict[str, str]]:
        resource = self.call_tool(name="resource.resolve", arguments={"project_id": project_id, "resource_id": resource_id})
        if rel:
            # A file referenced by the resource — e.g. a markdown relative
            # image link. Submitted figures (captured at associate, keyed by
            # the resource's pinned version) serve from the blob store; only
            # un-submitted links fall back to a repo-jailed live read, a
            # local-mode convenience.
            blob = self._submitted_figure(resource=resource, rel=rel)
            if blob is not None:
                data, name = blob
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                return data, {
                    "Content-Type": mime,
                    "Content-Disposition": f'inline; filename="{name}"',
                }
            # Not a submitted figure: in control mode there is no checkout to
            # read the live link from. Surface the documented degraded shape.
            if not self.expose_local_data_plane:
                raise ContentUnavailableError(
                    "figure bytes are unavailable in this mode",
                    details={"rel": rel, "reason": "content_unavailable_in_this_mode"},
                )
            path = self._resource_path(resource=resource)
            path = (path.parent / rel).resolve()
            try:
                path.relative_to(self.app.workspace.repo_root)
            except ValueError as exc:
                raise ValidationError("relative file path escapes repo root") from exc
            if not path.is_file():
                raise NotFoundError(f"file not found next to resource: {rel}")
        elif not self.expose_local_data_plane:
            raise ContentUnavailableError(
                "file bytes are unavailable in this mode",
                details={"reason": "content_unavailable_in_this_mode"},
            )
        else:
            path = self._resource_path(resource=resource)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path.read_bytes(), {
            "Content-Type": mime,
            "Content-Disposition": f'inline; filename="{path.name}"',
        }

    def _submitted_figure(
        self, *, resource: dict[str, Any], rel: str
    ) -> tuple[bytes, str] | None:
        version_id = self._latest_gated_version_id(resource=resource)
        if version_id is None:
            return None
        data = self.app.resources.submitted_figure(
            version_id=version_id, link_path=rel
        )
        if data is None:
            return None
        return data, rel.rsplit("/", 1)[-1]

    def create_project(
        self, body: dict[str, Any], *, tenant_id: str | None = None
    ) -> dict[str, Any]:
        name = body.get("name") or body.get("title") or "Untitled Project"
        summary = body.get("summary") or body.get("description") or body.get("research_goal") or ""
        # Route through call_tool (not the service directly) so HTTP-driven
        # project creation emits the same activity/tool_calls telemetry the MCP
        # path does — tenant_id rides in via internal_kwargs in hosted mode only.
        return self._present(
            self.app.call_tool(
                name="project.create",
                arguments={"name": name, "summary": summary},
                activity_source="http",
                internal_kwargs=(
                    {"tenant_id": tenant_id} if tenant_id is not None else None
                ),
            )
        )

    def create_experiment(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        name = body.get("name") or ""
        intent = body.get("intent") or body.get("title") or body.get("question") or ""
        claim_ids = body.get("tested_claim_ids") or body.get("claim_ids") or []
        return self.call_tool(name="experiment.create", arguments={"project_id": project_id, "name": name, "intent": intent, "tested_claim_ids": claim_ids})

    def register_resource(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        path = body.get("path")
        if not path:
            raise ValidationError("resource creation requires a repo-relative path")
        return self.call_tool(
            name="resource.register_file",
            arguments={
                "project_id": project_id,
                "path": path,
                "kind": body.get("kind", "other"),
                "title": body.get("title", ""),
                "created_by": body.get("created_by", "ui"),
            },
        )

    def filter_experiments(self, project_id: str, status: str | None) -> dict[str, Any]:
        # Full per-experiment state for the UI; the experiment.list tool stays slim.
        experiments = self.app.experiments.list_experiments(project_id=project_id)["experiments"]
        if status:
            experiments = [exp for exp in experiments if exp.get("status") == status]
        return {"experiments": experiments}

    def filter_resources(self, project_id: str, kind: str | None) -> dict[str, Any]:
        resources = self.call_tool(name="resource.list", arguments={"project_id": project_id})["resources"]
        if kind:
            resources = [res for res in resources if res.get("kind") == kind]
        return {"resources": resources}

    def _resource_path(self, *, resource: dict[str, Any]) -> Path:
        path = (self.app.workspace.repo_root / resource["path"]).resolve()
        try:
            path.relative_to(self.app.workspace.repo_root)
        except ValueError as exc:
            raise ValidationError("resource path escapes repo root") from exc
        if not path.exists() or not path.is_file():
            raise NotFoundError(f"resource file missing: {resource['path']}")
        return path

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
        return self._present({
            "sandboxes": [
                self.app.sandboxes.row_view(row=row)
                for row in self.app.sandboxes.rows(project_id=project_id)
            ]
        })

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

    def results_metrics_view(self, *, project_id: str, experiment_id: str) -> dict[str, Any]:
        """Centralized MLflow metrics for one experiment."""
        return self.app.mlflow_tracking.results_metrics(
            experiment_id=experiment_id, project_id=project_id
        )

    def mlflow_overview_view(self, *, project_id: str) -> dict[str, Any]:
        """Project-wide MLflow context for the UI.

        Agents should use mlflow.context and MLflow's native APIs for
        analysis. This view keeps the project-scoped dashboard operational when
        the central MLflow UI spans multiple projects.
        """
        health = self.app.mlflow_tracking.health()
        dashboard_url = str(health.get("dashboard_url") or "").rstrip("/")
        experiments = self.app.experiments.list_experiments(project_id=project_id)["experiments"]
        items: list[dict[str, Any]] = []
        for exp in experiments:
            eid = str(exp.get("id") or "")
            if not eid:
                continue
            metrics = self.app.mlflow_tracking.results_metrics(
                experiment_id=eid, project_id=project_id
            )
            mlflow_name = mlflow_experiment_name(project_id=project_id, experiment_id=eid)
            # Resolve MLflow's numeric experiment id from the snapshot so the UI
            # can deep-link the embedded drill-in to {dashboard}/#/experiments/<id>.
            captured = metrics.get("experiments") if isinstance(metrics, dict) else None
            numeric_id = ""
            for me in captured or []:
                if str(me.get("name") or "") == mlflow_name:
                    numeric_id = str(me.get("experiment_id") or "")
                    break
            if not numeric_id and captured:
                numeric_id = str(captured[0].get("experiment_id") or "")
            items.append({
                "experiment_id": eid,
                "name": exp.get("name") or eid,
                "status": exp.get("status") or "",
                "intent": exp.get("intent") or "",
                "mlflow_experiment_name": mlflow_name,
                "dashboard_experiment_url": (
                    f"{dashboard_url}/#/experiments/{numeric_id}"
                    if dashboard_url and numeric_id else ""
                ),
                "metrics": metrics,
            })
        return self._present({"mlflow": health, "experiments": items})

    def sandbox_health_view(self) -> dict[str, Any]:
        return self.app.sandboxes.backend_health()

    def experiment_figure(self, *, project_id: str, experiment_id: str) -> dict[str, Any]:
        """Derived figure graph for the UI canvas (no agent-authored overlay yet)."""
        experiment = self.app.experiments.get_state(experiment_id=experiment_id, project_id=project_id)
        review_attempts = {
            str(review.get("id")): int(
                self.app.reviews.snapshot_from_id(
                    snapshot_id=str(review.get("target_snapshot_id") or "")
                ).get("attempt_index") or 0
            )
            for review in experiment.get("reviews", [])
        }
        sandbox_row = self.app.sandboxes.get_row(experiment_id=experiment_id, project_id=project_id)
        sandbox = (
            self.app.sandboxes.row_view(row=sandbox_row)
            if sandbox_row is not None
            else None
        )
        return self._present(
            build_experiment_figure(
                experiment=experiment,
                review_attempts=review_attempts,
                open_review_requests=self.app.reviews.open_requests_for_target(
                    project_id=project_id, experiment_id=experiment_id
                ),
                sandbox=sandbox,
            ),
        )

    def experiment_logic_graph(self, *, project_id: str, experiment_id: str) -> dict[str, Any]:
        """Agent-authored logic graph (role 'graph'), parsed + envelope-linted.

        Prefers the current attempt's association; falls back to the latest
        prior-attempt one so the story stays visible right after an attempt
        bump, before the agent re-associates the file.
        """
        experiment = self.app.experiments.get_state(
            experiment_id=experiment_id, project_id=project_id
        )
        attempt = experiment.get("attempt_index")
        chosen = preferred_associated_resource(
            resources=experiment.get("resources", []),
            attempt=attempt,
            roles=("graph",),
        )
        base = {
            "experiment_id": experiment_id,
            "max_nodes": MAX_GRAPH_NODES,
            "experiment_status": experiment.get("status"),
            "attempt_index": attempt,
        }
        if chosen is None:
            return {**base, "available": False, "graph": None, "problems": []}
        text = self._association_pinned_text(chosen)
        if text is None:
            # The association predates byte capture (or the blob is gone):
            # degrade with re-associate guidance, never a 500.
            return {
                **base,
                "available": False,
                "graph": None,
                "problems": ["graph has no submitted content — re-associate it (role 'graph')"],
                "path": chosen.get("path"),
            }
        return self._graph_payload(
            base=base, chosen=chosen, text=text, project_id=project_id
        )

    def syntheses_view(self, *, project_id: str) -> dict[str, Any]:
        """All reflection waves plus the staleness/coverage signal for the UI."""
        return self.app.syntheses.overview(project_id=project_id)

    def synthesis_detail(self, *, project_id: str, synthesis_id: str) -> dict[str, Any]:
        return self.app.syntheses.get_state(synthesis_id=synthesis_id, project_id=project_id)

    def project_logic_graph(self, *, project_id: str) -> dict[str, Any]:
        """The living project logic graph (role 'project_graph' on a synthesis).

        Chooses the open wave's graph when one exists (so the user can watch
        the synthesis being written), else the latest published one. The same
        payload shape as the per-experiment graph endpoint so the UI renders
        both through one component.
        """
        selection = self.app.syntheses.project_logic_graph_selection(project_id=project_id)
        return self._graph_payload_for_synthesis(
            project_id=project_id,
            synthesis=selection.get("synthesis"),
            graph_resource=selection.get("graph_resource"),
            extra_base={"signal": selection.get("signal")},
        )

    def synthesis_graph(self, *, project_id: str, synthesis_id: str) -> dict[str, Any]:
        """The logic graph of one specific reflection wave, rendered from the
        bytes that wave pinned (role 'project_graph'). Lets the UI show a past
        wave's graph faithfully even though project/logic_graph.json is a
        living file the next wave overwrites. Same payload shape as
        project_logic_graph
        (minus the project-wide staleness signal)."""
        synthesis = self.app.syntheses.get_state(
            synthesis_id=synthesis_id, project_id=project_id
        )
        return self._graph_payload_for_synthesis(
            project_id=project_id, synthesis=synthesis
        )

    def _graph_payload_for_synthesis(
        self,
        *,
        project_id: str,
        synthesis: dict[str, Any] | None,
        graph_resource: dict[str, Any] | None = None,
        extra_base: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Shared graph shaping for the project-current and per-wave endpoints:
        pick the synthesis's graph association, load its pinned bytes, then
        parse + lint + resolve refs into the common payload the LogicGraph
        component renders. `extra_base` injects endpoint-specific fields (the
        project-current endpoint adds the staleness `signal`)."""
        base: dict[str, Any] = {"max_nodes": MAX_GRAPH_NODES, **(extra_base or {})}
        chosen = graph_resource or (
            self._synthesis_graph_resource(synthesis=synthesis) if synthesis else None
        )
        if synthesis is None or chosen is None:
            return {**base, "available": False, "synthesis": None, "graph": None, "problems": []}
        base["synthesis"] = {
            "id": synthesis.get("id"),
            "title": synthesis.get("title"),
            "status": synthesis.get("status"),
            "attempt_index": synthesis.get("attempt_index"),
            "published_at": synthesis.get("published_at"),
        }
        text = self._association_pinned_text(chosen)
        if text is None:
            return {
                **base,
                "available": False,
                "graph": None,
                "problems": [
                    "graph has no submitted content — re-associate it "
                    "(role 'project_graph')"
                ],
                "path": chosen.get("path"),
            }
        return self._graph_payload(
            base=base, chosen=chosen, text=text, project_id=project_id
        )

    def _graph_payload(
        self, *, base: dict[str, Any], chosen: dict[str, Any], text: str, project_id: str
    ) -> dict[str, Any]:
        """Parse + lint + resolve-refs the available-graph tail shared by the
        experiment and synthesis graph endpoints (byte-identical payload)."""
        graph: dict[str, Any] | None = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                graph = parsed
        except json.JSONDecodeError:
            graph = None
        return {
            **base,
            "available": True,
            "resource_id": chosen.get("id"),
            "path": chosen.get("path"),
            "association_attempt_index": chosen.get("association_attempt_index"),
            "graph": graph,
            "problems": graph_problems(text),
            "ref_index": self.app.graph_refs.resolve_index(
                project_id=project_id, graph=graph
            ),
        }

    def _association_pinned_text(self, resource: dict[str, Any]) -> str | None:
        """The submitted bytes behind a resources_for_target row (associated
        version → blob), or None when nothing was submitted."""
        return self.app.resources.submitted_text_for_version(
            version_id=resource.get("association_version_id")
        )

    def _synthesis_graph_resource(
        self, *, synthesis: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The synthesis's graph association — current attempt preferred, with
        the prior-attempt fallback the experiment endpoint also uses."""
        if synthesis is None:
            return None
        return preferred_associated_resource(
            resources=synthesis.get("resources", []),
            attempt=synthesis.get("attempt_index"),
            roles=PROJECT_GRAPH_ROLES,
        )

    def _experiment_view_model(self, *, exp: dict[str, Any]) -> dict[str, Any]:
        current = exp.get("current_attempt_resources", [])
        plans = [res["id"] for res in current if res.get("association_role") == "plan"]
        results = [res["id"] for res in current if res.get("association_role") == "result"]
        return {
            **exp,
            "tests_claims": [claim["id"] for claim in exp.get("tested_claims", [])],
            "input_resources": [res["id"] for res in current if res.get("association_role") in {"input", "code", "config", "plan"}],
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


def create_fastapi_app(
    app: ResearchPluginApp | None = None,
    *,
    router: ProjectRouter | None = None,
    allowed_origins: list[str] | None = None,
    task_queue: Any | None = None,
    cleanup: Any | None = None,
    surface_policy: HttpSurfacePolicy | None = None,
) -> FastAPI:
    # HTTP surface seam. Local mode exposes data-plane routes and accepts
    # repo_root context. Hosted control hides local data-plane routes and relies
    # on the local daemon/proxy to resolve repo_root -> project_id.
    if (app is None) == (router is None):
        raise ValueError("provide exactly one of app or router")
    surface = surface_policy or HttpSurfacePolicy.for_surface(
        restrict_cors=False,
        hosted_control=False,
        expose_local_data_plane=True,
    )
    api = (
        ResearchHttpApi(app=app, expose_local_data_plane=surface.expose_local_data_plane)
        if app is not None
        else None
    )

    def api_for_project(project_id: str) -> ResearchHttpApi:
        if router is not None:
            return ResearchHttpApi(
                app=router.app_for_project(project_id),
                expose_local_data_plane=surface.expose_local_data_plane,
            )
        assert api is not None
        return api

    def default_api() -> ResearchHttpApi | None:
        if api is not None:
            return api
        assert router is not None
        app = router.any_app()
        return (
            ResearchHttpApi(app=app, expose_local_data_plane=surface.expose_local_data_plane)
            if app is not None
            else None
        )

    def route_call_tool(
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        activity_source: str = "http",
        principal: Any | None = None,
    ) -> dict[str, Any]:
        arguments = dict(arguments or {})
        context = dict(context or {})
        contract = TOOL_CONTRACTS.get(name)

        def validated_contract_input() -> Any:
            assert contract is not None
            try:
                return contract.input_model.model_validate(arguments)
            except PydanticValidationError as exc:
                raise ValidationError(
                    "invalid tool arguments",
                    details={"tool": name, "errors": exc.errors()},
                ) from exc

        if not surface.accept_repo_root_context and context.get("repo_root"):
            raise DataPlaneRequiredError(
                "repo_root context is local data-plane state; hosted control "
                "requires the proxy or daemon to resolve and send project_id",
                details={
                    "field": "context.repo_root",
                    "reason": "repo_root_hidden_from_cloud",
                },
            )
        if not surface.allow_data_plane_tool_calls and name in DATA_PLANE_TOOL_NAMES:
            raise DataPlaneRequiredError(
                f"{name} requires the local data-plane daemon; hosted control "
                "mode cannot read local files, hold user SSH keys, or run rsync",
                details={
                    "tool": name,
                    "reason": "requires_local_data_plane",
                },
            )
        if router is not None:
            return router.call_tool(
                name=name,
                arguments=arguments,
                context=context,
                activity_source=activity_source,
            )
        assert api is not None
        policy = (
            HOSTED_CONTROL_TOOL_POLICIES.get(name)
            if surface.use_hosted_tool_policies
            else None
        )
        if policy is not None:
            call_kwargs: dict[str, Any] = {}
            if policy.telemetry_from_review_request:
                call_kwargs["telemetry_project_id"] = (
                    api.app.reviews.request_project_id(
                        review_request_id=arguments.get("review_request_id")
                    )
                )
            return api.app.call_tool(
                name=name,
                arguments=arguments,
                activity_source=activity_source,
                **call_kwargs,
            )
        if name in PROJECT_SCOPED_TOOL_NAMES and "project_id" not in arguments and (context or {}).get("repo_root"):
            projects = api.app.projects.list_projects()["projects"]
            if len(projects) != 1:
                raise ValidationError(
                    "project_id is required when the repo has multiple projects",
                    details={"projects": [project["id"] for project in projects]},
                )
            arguments["project_id"] = projects[0]["id"]
        if (
            surface.hosted_control
            and contract is not None
            and contract.hosted_control_sandbox_lookup
        ):
            request = validated_contract_input()
            result = api.app.sandboxes.get(
                experiment_id=request.experiment_id,
                project_id=request.project_id,
                tenant_id=None,
                # Address the specific sandbox by its durable id (decoupled
                # identity); omitted still targets the experiment's primary.
                sandbox_uid=request.sandbox_uid,
                include_data_plane_enrichment=False,
            )
            return result
        return api.app.call_tool(name=name, arguments=arguments, activity_source=activity_source)

    def require_data_plane_for_http(*, feature: str) -> None:
        tool = HTTP_DATA_PLANE_FEATURE_TO_TOOL[feature]
        if surface.allow_data_plane_http:
            return
        raise DataPlaneRequiredError(
            f"{tool} requires the local data-plane daemon; hosted control mode "
            "serves this API as an observer/admin surface",
            details={
                "tool": tool,
                "reason": "requires_local_data_plane",
            },
        )

    http = FastAPI(title="Research Plugin API", version=__version__)
    # CORS: local mode keeps the wide-open `*` policy (loopback-only).
    # Control mode uses an explicit allowed-origins list.
    if surface.restrict_cors:
        http.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins or [],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=UI_CORS_HEADERS,
        )
    else:
        http.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            # The hosted UI sends Authorization; every UI request also stamps
            # X-RP-Client-Version. Include both so dev overrides pass preflight.
            allow_headers=UI_CORS_HEADERS,
        )

    @http.middleware("http")
    async def attach_principal(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        request.state.principal = LOCAL_PRINCIPAL
        request.state.authenticated = False
        # /health is liveness; /api/meta is the version handshake itself.
        if request.url.path in ("/health", "/api/meta"):
            return await call_next(request)
        # Version/compat floor (cloud plan Phase 9). A below-floor client is
        # rejected with an actionable upgrade error. A missing header is
        # tolerated for pre-Phase-9 clients.
        client_version = request.headers.get(CLIENT_VERSION_HEADER)
        if surface.hosted_control and client_version and is_below_floor(
            client_version=client_version, floor=MIN_PROXY_VERSION
        ):
            return JSONResponse(
                {
                    "detail": (
                        f"client version {client_version} is below the minimum "
                        f"supported {MIN_PROXY_VERSION}; upgrade the research-plugin "
                        "client (pip install -U research-plugin) and reconnect"
                    ),
                    "error_code": "client_too_old",
                    "min_version": MIN_PROXY_VERSION,
                    "client_version": client_version,
                },
                status_code=426,
            )
        return await call_next(request)

    @http.middleware("http")
    async def log_http_activity(request: Request, call_next):
        started = monotonic_ms()
        status = 500
        # Per-request id for the structured cloud log stream (cloud plan
        # Phase 9). Echoed back on the response so a client/log line can be
        # correlated. Cheap stdlib uuid; no new dependency.
        import uuid

        request_id = uuid.uuid4().hex[:16]
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-RP-Request-Id"] = request_id
            return response
        finally:
            path = str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")
            duration_ms = monotonic_ms() - started
            if api is not None:
                # Intentionally only collect MCP tool-call events in the shared
                # activity log (HTTP request telemetry was disabled per request).
                # Structured cloud log line (control mode only; dormant locally).
                # tenant_id comes from the resolved principal when present.
                principal = getattr(request.state, "principal", None)
                api.app.structured_logger.log(
                    kind="http",
                    request_id=request_id,
                    tenant_id=getattr(principal, "tenant_id", "") or "",
                    path=str(request.url.path),
                    status=status,
                    duration_ms=duration_ms,
                    method=request.method,
                )

    @http.exception_handler(ResearchPluginError)
    async def research_error_handler(_request: Request, exc: ResearchPluginError) -> JSONResponse:
        # 404 for missing records AND for content-unavailable (the bytes live on
        # an offline daemon / are metadata-only): the error_code lets the UI
        # render an explicit degraded state rather than a generic error.
        status = 404 if isinstance(exc, (NotFoundError, ContentUnavailableError)) else 400
        return JSONResponse({"detail": exc.message, "error_code": exc.error_code, **exc.details}, status_code=status)

    @http.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "invalid HTTP request", "errors": exc.errors()}, status_code=400)

    @http.get("/health")
    def health() -> dict[str, Any]:
        # Surface hygiene (cloud plan Phase 7): /health leaks machine-local
        # paths (repo_root, store path, registry path). Local mode keeps the
        # rich shape (loopback, single user). Control mode returns a slim
        # liveness shape — no host paths cross the cloud edge.
        if not surface.expose_local_data_plane:
            return {"ok": True, "version": __version__}
        if router is not None:
            return {"ok": True, "version": __version__, **router.health()}
        assert api is not None
        return api.health()

    @http.get("/api/meta")
    def server_meta() -> dict[str, Any]:
        # Version/compat handshake (cloud plan Phase 9): the server version plus
        # the minimum daemon/proxy versions it will serve. Floors are code
        # constants; mode/capabilities tell browser clients which local
        # data-plane actions to hide before requests start getting rejected.
        payload = meta()
        payload["mode"] = "control" if surface.hosted_control else "local"
        payload["capabilities"] = {
            "hosted_control": surface.hosted_control,
            "local_data_plane_http": surface.allow_data_plane_http,
            **surface.data_plane_http_capabilities(),
        }
        return payload

    @http.get("/api/activity")
    def activity(
        request: Request,
        limit: int = Query(100, ge=1),
        source: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if router is not None:
            return router.activity_recent(limit=limit, source=source, project_id=project_id)
        assert api is not None
        return api.activity(limit=limit, source=source, project_id=project_id)

    # /api/debug/* expose tool-call internals. Hosted control is currently a
    # private operator surface; the real auth system should gate these before
    # broad exposure.
    @http.get("/api/debug/tool-calls")
    def tool_call_stats(
        request: Request,
        minutes: int | None = Query(None, ge=1),
        source: str | None = None,
        status: str | None = None,
        tool: str | None = None,
        project_id: str | None = None,
        limit: int = Query(200, ge=1, le=2000),
        sort: str = "ts",
        order: str = "desc",
    ) -> dict[str, Any]:
        target = default_api()
        if target is None:
            # Mirror ToolCallStore.stats' empty `base` shape so the UI renders
            # the same whether the store is empty or no app exists yet.
            return {
                "calls": [],
                "by_tool": [],
                "totals": {"calls": 0, "sent_chars": 0, "received_chars": 0, "error_calls": 0},
                "coverage": {"calls": 0, "stored": 0, "oldest_ts": None, "newest_ts": None, "capped": False},
                "filter": {"minutes": minutes, "source": source, "status": status, "tool": tool, "project_id": project_id},
            }
        return target.tool_call_stats(
            minutes=minutes,
            source=source,
            status=status,
            tool=tool,
            project_id=project_id,
            project_ids=None,
            limit=limit, sort=sort, order=order,
        )

    @http.get("/api/debug/tool-calls/{call_id}")
    def tool_call_detail(call_id: int, request: Request) -> dict[str, Any]:
        target = default_api()
        if target is None:
            raise NotFoundError("no project instantiated yet")
        return target.tool_call_detail(
            call_id=call_id,
            project_ids=None,
        )

    @http.post("/api/debug/tool-calls/clear")
    def tool_calls_clear(request: Request) -> dict[str, Any]:
        target = default_api()
        if target is None:
            return {"cleared": 0}
        return target.tool_calls_clear(project_ids=None)

    @http.get("/api/projects")
    def list_projects(request: Request) -> dict[str, Any]:
        if router is not None:
            return router.list_projects()
        assert api is not None
        return api.call_tool(name="project.list", arguments={})

    @http.post("/api/projects", status_code=201)
    def create_project(request: Request, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        payload = body or {}
        if router is not None:
            repo_root = payload.get("repo_root") or payload.get("directory") or payload.get("path")
            if not repo_root:
                raise ValidationError("repo_root is required", details={"field": "repo_root"})
            name = payload.get("name") or payload.get("title") or "Untitled Project"
            summary = payload.get("summary") or payload.get("description") or payload.get("research_goal") or ""
            return router.create_project(
                repo_root=repo_root,
                name=name,
                summary=summary,
            )
        assert api is not None
        return api.create_project(body=payload, tenant_id=None)

    @http.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="project.get", arguments={"project_id": project_id})

    @http.patch("/api/projects/{project_id}")
    @http.put("/api/projects/{project_id}")
    def update_project(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="project.update", arguments={"project_id": project_id, **(body or {})})

    @http.get("/api/projects/{project_id}/home")
    def home(project_id: str) -> dict[str, Any]:
        return api_for_project(project_id).home(project_id=project_id)

    @http.get("/api/projects/{project_id}/status")
    def project_status(project_id: str, experiment_id: str | None = None) -> dict[str, Any]:
        # Full shape for the UI (see home()); the tool stays slim for the agent.
        target = api_for_project(project_id)
        return target._present(
            target.app.workflow.status_and_next(
                project_id=project_id, experiment_id=experiment_id
            )
        )

    @http.get("/api/projects/{project_id}/claims")
    def list_claims(project_id: str) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="claim.list", arguments={"project_id": project_id})

    @http.post("/api/projects/{project_id}/claims", status_code=201)
    def create_claim(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="claim.create", arguments={"project_id": project_id, **(body or {})})

    @http.get("/api/projects/{project_id}/claims/{claim_id}")
    def get_claim(project_id: str, claim_id: str) -> dict[str, Any]:
        return api_for_project(project_id).get_claim(project_id=project_id, claim_id=claim_id)

    @http.patch("/api/projects/{project_id}/claims/{claim_id}")
    @http.put("/api/projects/{project_id}/claims/{claim_id}")
    def update_claim(project_id: str, claim_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="claim.update", arguments={"project_id": project_id, "claim_id": claim_id, **(body or {})})

    @http.get("/api/projects/{project_id}/experiments")
    def list_experiments(project_id: str, status: str | None = None) -> dict[str, Any]:
        return api_for_project(project_id).filter_experiments(project_id=project_id, status=status)

    @http.post("/api/projects/{project_id}/experiments", status_code=201)
    def create_experiment(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api_for_project(project_id).create_experiment(project_id=project_id, body=body or {})

    @http.get("/api/projects/{project_id}/experiments/view")
    def experiments_view(project_id: str) -> dict[str, Any]:
        return api_for_project(project_id).experiments_view(project_id=project_id)

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}")
    def get_experiment(project_id: str, experiment_id: str) -> dict[str, Any]:
        # Full shape for the UI; the experiment.get_state tool stays slim for the agent.
        return api_for_project(project_id).app.experiments.get_state(experiment_id=experiment_id, project_id=project_id)

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/status")
    def experiment_status(project_id: str, experiment_id: str) -> dict[str, Any]:
        # Full shape for the UI (see home()); the tool stays slim for the agent.
        target = api_for_project(project_id)
        return target._present(
            target.app.workflow.status_and_next(
                project_id=project_id, experiment_id=experiment_id
            )
        )

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/figure")
    def experiment_figure(project_id: str, experiment_id: str) -> dict[str, Any]:
        # Derived graph for the figure canvas; UI-only read, no agent tool.
        return api_for_project(project_id).experiment_figure(project_id=project_id, experiment_id=experiment_id)

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/graph")
    def experiment_logic_graph(project_id: str, experiment_id: str) -> dict[str, Any]:
        # Agent-authored logic graph (role 'graph'); UI-only read, no agent tool.
        return api_for_project(project_id).experiment_logic_graph(project_id=project_id, experiment_id=experiment_id)

    @http.post("/api/projects/{project_id}/experiments/{experiment_id}/transition")
    def transition_experiment(project_id: str, experiment_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="experiment.transition", arguments={"project_id": project_id, "experiment_id": experiment_id, **(body or {})})

    @http.get("/api/projects/{project_id}/syntheses")
    def list_syntheses(project_id: str) -> dict[str, Any]:
        # Reflection waves + staleness/coverage signal for the UI panel.
        return api_for_project(project_id).syntheses_view(project_id=project_id)

    @http.get("/api/projects/{project_id}/syntheses/current/graph")
    def project_logic_graph(project_id: str) -> dict[str, Any]:
        # The living project logic graph; same payload shape as the
        # per-experiment graph endpoint. UI-only read, no agent tool.
        return api_for_project(project_id).project_logic_graph(project_id=project_id)

    @http.get("/api/projects/{project_id}/syntheses/{synthesis_id}/graph")
    def synthesis_graph(project_id: str, synthesis_id: str) -> dict[str, Any]:
        # One wave's logic graph, rendered from the bytes that wave pinned, so
        # a past wave shows faithfully even after later waves overwrite the
        # living file. Same payload shape as /syntheses/current/graph (minus
        # signal). Registered after the literal current/graph route so
        # "current" is not captured as a synthesis_id. UI-only read.
        return api_for_project(project_id).synthesis_graph(
            project_id=project_id, synthesis_id=synthesis_id
        )

    @http.get("/api/projects/{project_id}/syntheses/{synthesis_id}")
    def get_synthesis(project_id: str, synthesis_id: str) -> dict[str, Any]:
        return api_for_project(project_id).synthesis_detail(
            project_id=project_id, synthesis_id=synthesis_id
        )

    @http.get("/api/projects/{project_id}/resources")
    def list_resources(project_id: str, kind: str | None = None) -> dict[str, Any]:
        return api_for_project(project_id).filter_resources(project_id=project_id, kind=kind)

    @http.post("/api/projects/{project_id}/resources", status_code=201)
    def register_resource(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        require_data_plane_for_http(feature="resource_registration")
        return api_for_project(project_id).register_resource(project_id=project_id, body=body or {})

    @http.get("/api/projects/{project_id}/resources/tree")
    def resources_tree(project_id: str) -> dict[str, Any]:
        return api_for_project(project_id).resources_tree(project_id=project_id)

    @http.get("/api/projects/{project_id}/resources/{resource_id}")
    def resolve_resource(project_id: str, resource_id: str) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="resource.resolve", arguments={"project_id": project_id, "resource_id": resource_id})

    @http.get("/api/projects/{project_id}/resources/{resource_id}/history")
    def resource_history(project_id: str, resource_id: str) -> dict[str, Any]:
        # UI-only read; the agent tool surface folds history into
        # resource.resolve(include_history=true), so call the service directly.
        return api_for_project(project_id).app.resources.history(
            resource_id=resource_id, project_id=project_id
        )

    @http.post("/api/projects/{project_id}/resources/{resource_id}/associate")
    def associate_resource(project_id: str, resource_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        require_data_plane_for_http(feature="resource_association")
        return api_for_project(project_id).call_tool(name="resource.associate", arguments={"project_id": project_id, "resource_id": resource_id, **(body or {})})

    @http.delete("/api/projects/{project_id}/resources/{resource_id}")
    def delete_resource(project_id: str, resource_id: str) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="resource.delete", arguments={"project_id": project_id, "resource_id": resource_id})

    def storage_for_project(project_id: str) -> Any:
        storage = api_for_project(project_id).app.storage
        if storage is None:
            raise NotFoundError("storage is not enabled on this backend")
        return storage

    @http.get("/api/projects/{project_id}/storage")
    def list_storage(
        project_id: str,
        kind: str | None = None,
        status: str | None = None,
        name: str | None = None,
        include_expired: bool = False,
    ) -> dict[str, Any]:
        return storage_for_project(project_id).list_objects(
            project_id=project_id,
            kind=kind,
            status=status,
            name=name,
            include_expired=include_expired,
        )

    @http.get("/api/projects/{project_id}/storage/{object_id}")
    def get_storage_object(project_id: str, object_id: str) -> dict[str, Any]:
        return storage_for_project(project_id).get_object(
            project_id=project_id, object_id=object_id
        )

    @http.post("/api/projects/{project_id}/storage/{object_id}/download")
    def download_storage_object(project_id: str, object_id: str) -> dict[str, Any]:
        return storage_for_project(project_id).resolve(
            project_id=project_id, object_id=object_id, include_download=True
        )

    @http.post("/api/projects/{project_id}/storage/{object_id}/pin")
    def pin_storage_object(project_id: str, object_id: str) -> dict[str, Any]:
        return {"object": storage_for_project(project_id).pin(
            project_id=project_id, object_id=object_id
        )}

    @http.post("/api/projects/{project_id}/storage/{object_id}/unpin")
    def unpin_storage_object(project_id: str, object_id: str) -> dict[str, Any]:
        return {"object": storage_for_project(project_id).unpin(
            project_id=project_id, object_id=object_id
        )}

    @http.post("/api/projects/{project_id}/storage/{object_id}/renew")
    def renew_storage_object(project_id: str, object_id: str) -> dict[str, Any]:
        return {"object": storage_for_project(project_id).renew(
            project_id=project_id, object_id=object_id
        )}

    @http.delete("/api/projects/{project_id}/storage/{object_id}")
    def delete_storage_object(project_id: str, object_id: str) -> dict[str, Any]:
        return storage_for_project(project_id).delete(
            project_id=project_id, object_id=object_id
        )

    @http.get("/api/projects/{project_id}/resources/{resource_id}/content")
    def resource_content(project_id: str, resource_id: str, version: str | None = None) -> dict[str, Any]:
        # `version` pins the exact submitted bytes of one resource version
        # (faithful historical rendering for reflection-wave synthesis
        # artifacts).
        # Omitted → unchanged behavior (latest gated bytes / live file).
        return api_for_project(project_id).resource_content(
            project_id=project_id, resource_id=resource_id, version=version
        )

    @http.get("/api/projects/{project_id}/resources/{resource_id}/file")
    def resource_file(project_id: str, resource_id: str, rel: str | None = None) -> Response:
        content, headers = api_for_project(project_id).resource_file(
            project_id=project_id, resource_id=resource_id, rel=rel
        )
        content_type = headers.pop("Content-Type", "application/octet-stream")
        return Response(content=content, media_type=content_type, headers=headers)

    @http.get("/api/projects/{project_id}/reviews")
    def reviews(project_id: str, target_type: str = "experiment", target_id: str | None = None) -> dict[str, Any]:
        if not target_id:
            return api_for_project(project_id).review_queue(project_id=project_id)
        return api_for_project(project_id).call_tool(name="review.status", arguments={"project_id": project_id, "target_type": target_type, "target_id": target_id})

    @http.post("/api/projects/{project_id}/reviews/request", status_code=201)
    def request_review(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="review.request", arguments={"project_id": project_id, **(body or {})})

    @http.post("/api/projects/{project_id}/reviews/start")
    def start_review(
        project_id: str,
        request: Request,
        body: JsonBody = Body(default=None),
    ) -> dict[str, Any]:
        return api_for_project(project_id).start_review(
            project_id=project_id,
            body=body or {},
            tenant_id=None,
        )

    @http.post("/api/projects/{project_id}/reviews/submit")
    def submit_review(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api_for_project(project_id).submit_review(project_id=project_id, body=body or {})

    @http.get("/api/projects/{project_id}/sandboxes")
    def list_sandboxes(project_id: str) -> dict[str, Any]:
        return api_for_project(project_id).sandbox_list_view(project_id=project_id)

    @http.get("/api/sandboxes/health")
    def sandbox_health() -> dict[str, Any]:
        if router is not None:
            return {"ok": True, "mode": "multi_project"}
        assert api is not None
        return api.sandbox_health_view()

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/sandbox")
    def get_sandbox(
        project_id: str, experiment_id: str, sandbox_uid: str | None = None
    ) -> dict[str, Any]:
        return api_for_project(project_id).sandbox_get_view(
            project_id=project_id, experiment_id=experiment_id, sandbox_uid=sandbox_uid
        )

    @http.get("/api/projects/{project_id}/sandboxes/{sandbox_uid}")
    def get_sandbox_by_uid(project_id: str, sandbox_uid: str) -> dict[str, Any]:
        return api_for_project(project_id).sandbox_get_view(
            project_id=project_id, sandbox_uid=sandbox_uid
        )

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/sandbox/metrics")
    def sandbox_metrics(
        project_id: str, experiment_id: str, sandbox_uid: str | None = None
    ) -> dict[str, Any]:
        return api_for_project(project_id).sandbox_metrics_view(
            project_id=project_id, experiment_id=experiment_id, sandbox_uid=sandbox_uid
        )

    @http.get("/api/projects/{project_id}/sandboxes/{sandbox_uid}/metrics")
    def sandbox_metrics_by_uid(project_id: str, sandbox_uid: str) -> dict[str, Any]:
        return api_for_project(project_id).sandbox_metrics_view(
            project_id=project_id, sandbox_uid=sandbox_uid
        )

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/results/metrics")
    def experiment_results_metrics(project_id: str, experiment_id: str) -> dict[str, Any]:
        return api_for_project(project_id).results_metrics_view(
            project_id=project_id, experiment_id=experiment_id
        )

    @http.get("/api/projects/{project_id}/mlflow")
    def project_mlflow(project_id: str) -> dict[str, Any]:
        return api_for_project(project_id).mlflow_overview_view(project_id=project_id)

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/sandbox/terminal")
    def sandbox_terminal(
        project_id: str,
        experiment_id: str,
        tail: int | None = None,
        since: int | None = None,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"project_id": project_id, "experiment_id": experiment_id}
        if sandbox_uid:
            args["sandbox_uid"] = sandbox_uid
        if tail is not None:
            args["tail"] = tail
        if since is not None:
            args["since"] = since
        return api_for_project(project_id).call_tool(name="sandbox.terminal", arguments=args)

    @http.get("/api/projects/{project_id}/sandboxes/{sandbox_uid}/terminal")
    def sandbox_terminal_by_uid(
        project_id: str,
        sandbox_uid: str,
        tail: int | None = None,
        since: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"project_id": project_id, "sandbox_uid": sandbox_uid}
        if tail is not None:
            args["tail"] = tail
        if since is not None:
            args["since"] = since
        return api_for_project(project_id).call_tool(name="sandbox.terminal", arguments=args)

    @http.post("/api/projects/{project_id}/experiments/{experiment_id}/sandbox/release")
    def release_sandbox(
        project_id: str,
        experiment_id: str,
        request: Request,
        sandbox_uid: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "project_id": project_id,
            "experiment_id": experiment_id,
            "confirm_retained": True,
        }
        if sandbox_uid:
            arguments["sandbox_uid"] = sandbox_uid
        return route_call_tool(
            name="sandbox.release",
            # The browser already confirms in its own UX; the retention gate is
            # for the agent's MCP call, so the UI route terminates directly.
            arguments=arguments,
            activity_source="http",
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )

    @http.post("/api/projects/{project_id}/sandboxes/{sandbox_uid}/release")
    def release_sandbox_by_uid(
        project_id: str,
        sandbox_uid: str,
        request: Request,
    ) -> dict[str, Any]:
        return route_call_tool(
            name="sandbox.release",
            arguments={
                "project_id": project_id,
                "sandbox_uid": sandbox_uid,
                "confirm_retained": True,
            },
            activity_source="http",
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )

    @http.get("/api/projects/{project_id}/events")
    def events(project_id: str, limit: int = Query(100, ge=1)) -> dict[str, Any]:
        return api_for_project(project_id).events(project_id=project_id, limit=limit)

    # Social feed (Feed_PRD.md) — a self-contained module: its routes register
    # themselves here, reading only off app.feed. Removing the feed is deleting
    # the feed package plus this one line.
    def app_for_feed(project_id: str, request: Request) -> ResearchPluginApp:
        return api_for_project(project_id).app

    register_feed_routes(http, app_for=app_for_feed)

    def list_mcp_tools() -> list[dict[str, Any]]:
        if router is not None:
            return router.list_tools()
        assert api is not None
        return api.app.list_tools()

    def call_mcp_tool(
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        request: Request,
    ) -> dict[str, Any]:
        return route_call_tool(
            name=name,
            arguments=arguments,
            context=context,
            activity_source="mcp",
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )

    register_mcp_routes(
        http,
        list_tools=list_mcp_tools,
        call_tool=call_mcp_tool,
        allow_tool=(
            None
            if surface.allow_data_plane_tool_calls
            else lambda tool: tool.get("plane") != "data"
        ),
    )

    def app_for_daemon_project(request: Request, project_id: str) -> ResearchPluginApp:
        return api_for_project(project_id).app

    register_daemon_routes(
        http,
        task_queue=task_queue,
        app_for_project=app_for_daemon_project,
    )

    # Control-admin routes: optional cleanup trigger plus tenant counters.
    register_admin_routes(
        http,
        store=api.app.store if api is not None else None,
        cleanup=cleanup,
    )

    return http
