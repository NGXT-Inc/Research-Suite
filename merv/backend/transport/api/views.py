"""HTTP presentation helpers for the Merv API."""

from __future__ import annotations

import json
import mimetypes
from typing import Any

from ... import __version__
from ...artifacts.figure_view import build_experiment_figure
from ...artifacts.resource_selection import preferred_associated_resource
from ...artifacts.roles import GATED_ROLES, PROJECT_GRAPH_ROLES
from ...domain.graph_lint import MAX_GRAPH_NODES, graph_problems
from ...mlflow import mlflow_experiment_name, mlflow_visible_for_status
from ...sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES
from ...state.activity import effective_source, is_event_ok
from ...utils import (
    ContentUnavailableError,
    NotFoundError,
    ValidationError,
    WorkflowError,
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

    def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        internal_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._present(
            self.app.call_tool(
                name=name,
                arguments=arguments,
                activity_source="http",
                internal_kwargs=internal_kwargs,
            )
        )

    def _present(self, value: Any) -> Any:
        return self._strip_local_data_plane(value)

    def experiment_state_view(self, *, project_id: str, experiment_id: str) -> dict[str, Any]:
        state = self.app.experiments.get_state(
            experiment_id=experiment_id,
            project_id=project_id,
        )
        if not mlflow_visible_for_status(state.get("status")):
            return self._present(state)
        enriched = dict(state)
        mlflow = self.app.mlflow_tracking.context(
            project_id=project_id,
            experiment_id=experiment_id,
        ).to_dict()
        run = state.get("mlflow_run")
        if isinstance(run, dict):
            mlflow["run"] = run
            run_id = str(run.get("run_id") or "")
            if run_id:
                env = dict(mlflow.get("env") or {})
                env["MLFLOW_RUN_ID"] = run_id
                env["RP_MLFLOW_RUN_ID"] = run_id
                mlflow["env"] = env
        enriched["mlflow"] = mlflow
        return self._present(enriched)

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
        return {"ok": True, "version": __version__}

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
        resources = self.call_tool(name="resource.find", arguments={"project_id": project_id})["resources"]
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
        resources = self.call_tool(name="resource.find", arguments={"project_id": project_id})["resources"]
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
        resource = self.call_tool(name="resource.find", arguments={"project_id": project_id, "resource_id": resource_id})
        # An explicit version pins the EXACT submitted bytes of one resource
        # version (used by the reflection-wave UI to render a past wave's
        # graph, reflection doc, or change spec faithfully, not the latest
        # living file).
        if version:
            return self._resource_content_at_version(
                project_id=project_id, resource=resource, version_id=version
            )
        # Gated-role artifacts render their submitted bytes (the content the
        # gates lint and reviewers grade). Other roles are metadata-only here;
        # raw file reads live in the local MCP proxy.
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
        return {
            "resource": resource,
            "path": resource.get("path"),
            "content": None,
            "text": None,
            "available": False,
            "source": "unavailable",
            "reason": "content_unavailable_in_this_mode",
            "detail": (
                "this file's bytes live only on the local data plane; "
                "result-role files are metadata-only in this mode"
            ),
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
        resource = self.call_tool(name="resource.find", arguments={"project_id": project_id, "resource_id": resource_id})
        if rel:
            # A file referenced by the resource, e.g. a markdown relative image
            # link. Submitted figures serve from the blob store; uncaptured
            # links are local data-plane only.
            blob = self._submitted_figure(resource=resource, rel=rel)
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
        self, body: dict[str, Any], *, tenant_id: str | None = None, user_id: str = ""
    ) -> dict[str, Any]:
        name = body.get("name") or body.get("title") or "Untitled Project"
        summary = body.get("summary") or body.get("description") or body.get("research_goal") or ""
        # Route through call_tool (not the service directly) so HTTP-driven
        # project creation emits the same activity/tool_calls telemetry the MCP
        # path does — tenant_id/user_id ride in via internal_kwargs in hosted
        # mode only (the creator becomes the project's first member).
        internal: dict[str, Any] = {}
        if tenant_id is not None:
            internal["tenant_id"] = tenant_id
        if user_id:
            internal["user_id"] = user_id
        return self._present(
            self.app.call_tool(
                name="project",
                arguments={"action": "create", "name": name, "summary": summary},
                activity_source="http",
                internal_kwargs=internal or None,
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
            name="resource.register",
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
        resources = self.call_tool(name="resource.find", arguments={"project_id": project_id})["resources"]
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
        return self._present({
            "sandboxes": [
                self.app.sandboxes.row_view(row=row)
                for row in self.app.sandboxes.rows(project_id=project_id)
            ]
        })

    def compute_cost_view(self, *, project_id: str) -> dict[str, Any]:
        """Project compute spend for the UI, experiment names hydrated.

        Sourced from the sandbox_generations ledger (not live sandbox rows),
        so terminated fleets keep counting toward the total.
        """
        spend = self.app.quotas.project_spend(project_id=project_id)
        experiments = self.app.experiments.list_experiments(project_id=project_id)["experiments"]
        names = {str(exp.get("id") or ""): str(exp.get("name") or "") for exp in experiments}
        for entry in spend["by_experiment"]:
            entry["experiment_name"] = names.get(entry["experiment_id"], "")
        return self._present(spend)

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
        experiments = self.app.experiments.list_experiments(project_id=project_id)["experiments"]
        unreachable = health.get("reachable") is False
        items: list[dict[str, Any]] = []
        for exp in experiments:
            eid = str(exp.get("id") or "")
            if not eid:
                continue
            # results_metrics owns the deep-link (namespace→#/experiments/<id>).
            metrics = (
                {
                    "experiment_id": eid,
                    "available": False,
                    "source": "mlflow",
                    "hint": "MLflow unreachable.",
                }
                if unreachable
                else self.app.mlflow_tracking.results_metrics(
                    experiment_id=eid,
                    project_id=project_id,
                    include_history=False,
                )
            )
            items.append({
                "experiment_id": eid,
                "name": exp.get("name") or eid,
                "status": exp.get("status") or "",
                "intent": exp.get("intent") or "",
                "mlflow_experiment_name": mlflow_experiment_name(
                    project_id=project_id, experiment_id=eid
                ),
                "dashboard_experiment_url": (
                    metrics.get("dashboard_experiment_url", "")
                    if isinstance(metrics, dict) else ""
                ),
                "metrics": metrics,
            })
        expected_names = {str(item["mlflow_experiment_name"]) for item in items}
        namespace_experiments = (
            []
            if unreachable
            else self.app.mlflow_tracking.namespace_experiments(project_id=project_id)
        )
        unmapped = [
            experiment
            for experiment in namespace_experiments
            if str(experiment.get("name") or "") not in expected_names
        ]
        return self._present({
            "mlflow": health,
            "experiments": items,
            "unmapped_mlflow_experiments": unmapped,
        })

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
                # Liveness verdict stays with the sandbox module's vocabulary.
                sandbox_active=bool(
                    sandbox
                    and str(sandbox.get("status") or "") in ACTIVE_SANDBOX_STATUSES
                ),
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

    def reflections_view(self, *, project_id: str) -> dict[str, Any]:
        """All reflection waves plus the staleness/coverage signal for the UI."""
        return self.app.reflection_waves.overview(project_id=project_id)

    def reflection_detail(self, *, project_id: str, synthesis_id: str) -> dict[str, Any]:
        return self.app.reflection_waves.get_state(
            synthesis_id=synthesis_id, project_id=project_id
        )

    def project_logic_graph(self, *, project_id: str) -> dict[str, Any]:
        """The living project logic graph (role 'project_graph' on a reflection).

        Chooses the open wave's graph when one exists (so the user can watch
        the reflection being written), else the latest published one. The same
        payload shape as the per-experiment graph endpoint so the UI renders
        both through one component.
        """
        selection = self.app.reflection_waves.project_logic_graph_selection(project_id=project_id)
        return self._graph_payload_for_reflection(
            project_id=project_id,
            synthesis=selection.get("synthesis"),
            graph_resource=selection.get("graph_resource"),
            extra_base={"signal": selection.get("signal")},
        )

    def reflection_graph(self, *, project_id: str, synthesis_id: str) -> dict[str, Any]:
        """The logic graph of one specific reflection wave, rendered from the
        bytes that wave pinned (role 'project_graph'). Lets the UI show a past
        wave's graph faithfully even though project/logic_graph.json is a
        living file the next wave overwrites. Same payload shape as
        project_logic_graph
        (minus the project-wide staleness signal)."""
        synthesis = self.app.reflection_waves.get_state(
            synthesis_id=synthesis_id, project_id=project_id
        )
        return self._graph_payload_for_reflection(
            project_id=project_id, synthesis=synthesis
        )

    def _graph_payload_for_reflection(
        self,
        *,
        project_id: str,
        synthesis: dict[str, Any] | None,
        graph_resource: dict[str, Any] | None = None,
        extra_base: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Shared graph shaping for the project-current and per-wave endpoints:
        pick the reflection's graph association, load its pinned bytes, then
        parse + lint + resolve refs into the common payload the LogicGraph
        component renders. `extra_base` injects endpoint-specific fields (the
        project-current endpoint adds the staleness `signal`)."""
        base: dict[str, Any] = {"max_nodes": MAX_GRAPH_NODES, **(extra_base or {})}
        chosen = graph_resource or (
            self._reflection_graph_resource(synthesis=synthesis) if synthesis else None
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

    def _reflection_graph_resource(
        self, *, synthesis: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The reflection's graph association — current attempt preferred, with
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
