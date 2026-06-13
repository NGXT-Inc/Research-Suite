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

from . import __version__
from .app import ResearchPluginApp
from .contracts import PROJECT_SCOPED_TOOL_NAMES
from .project_router import ProjectRouter
from .services.figure_view import build_experiment_figure
from .services.graph_lint import MAX_GRAPH_NODES, graph_problems
from .services.identity import LOCAL_PRINCIPAL, AuthError, AuthService
from .services.permissions import GATED_ROLES
from .utils import NotFoundError, ResearchPluginError, ValidationError
from .state import monotonic_ms


JsonBody = dict[str, Any] | None


def _latest_graph_resource(
    *, resources: list[dict[str, Any]], attempt: Any
) -> dict[str, Any] | None:
    """The graph association to render: current attempt preferred (prior
    attempts keep the story visible right after an attempt bump), most
    recently associated within the pool. Recency matches the transition
    validator's ORDER BY a.created_seq DESC, so the UI never renders a
    different file than the gate lints (the association_rowid key kept its
    historical name when created_seq replaced rowid ordering)."""
    candidates = [r for r in resources if r.get("association_role") == "graph"]
    if not candidates:
        return None
    current = [r for r in candidates if r.get("association_attempt_index") == attempt]
    pool = current or candidates
    return max(pool, key=lambda r: r.get("association_rowid") or 0)


class ResearchHttpApi:
    """HTTP view helpers over ResearchPluginApp. Domain logic stays in services."""

    def __init__(self, *, app: ResearchPluginApp) -> None:
        self.app = app

    def call_tool(self, *, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.app.call_tool(name=name, arguments=arguments, activity_source="http")

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "repo_root": str(self.app.workspace.repo_root),
            "store": str(self.app.store.db_path),
            "activity_log": str(self.app.activity.log_path),
        }

    def activity(self, limit: int, source: str | None = None) -> dict[str, Any]:
        result = self.app.activity.recent(limit=limit, source=source)
        return {
            "activity_log": str(self.app.activity.log_path),
            "filter": {"source": source} if source else {},
            "events": result["events"],
            "summary": result["summary"],
        }

    def tool_call_stats(
        self,
        *,
        minutes: int | None,
        source: str | None,
        status: str | None,
        tool: str | None,
        limit: int,
        sort: str,
        order: str,
    ) -> dict[str, Any]:
        return self.app.tool_calls.stats(
            minutes=minutes,
            source=source,
            status=status,
            tool=tool,
            limit=limit,
            sort=sort,
            order=order,
        )

    def tool_call_detail(self, call_id: int) -> dict[str, Any]:
        record = self.app.tool_calls.get(call_id=call_id)
        if record is None:
            raise NotFoundError(f"tool call not found: {call_id}")
        return record

    def tool_calls_clear(self) -> dict[str, Any]:
        return self.app.tool_calls.clear()

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
        return {
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
        }

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

    def start_review(self, *, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.app.reviews.assert_request_in_project(
            project_id=project_id, review_request_id=body.get("review_request_id")
        )
        # Phase 8: thread request.state.principal.tenant_id into reviews.start so
        # the cross-tenant binding (already enforced at the service layer, cloud
        # plan Phase 7) is checked over the HTTP/proxy boundary too. Local mode
        # is the single 'local' tenant, so the default-None path is correct now.
        return self.call_tool(name="review.start", arguments=body)

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

    def resource_content(self, project_id: str, resource_id: str) -> dict[str, Any]:
        resource = self.call_tool(name="resource.resolve", arguments={"project_id": project_id, "resource_id": resource_id})
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
        path = self._resource_path(resource=resource)
        text = path.read_text(errors="replace")
        return {
            "resource": resource,
            "path": resource["path"],
            "content": text,
            "text": text,
            "size_bytes": path.stat().st_size,
            "source": "live",
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
        version_id = self._latest_gated_version_id(resource=resource)
        if version_id is None:
            return None
        conn = self.app.store.connect()
        try:
            row = conn.execute(
                "SELECT project_id, content_sha256 FROM resource_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        try:
            data = self.app.blobs.get(
                namespace=str(row["project_id"]), sha256=str(row["content_sha256"])
            )
        except NotFoundError:
            return None
        return data.decode("utf-8", errors="replace"), version_id

    def resource_file(
        self, project_id: str, resource_id: str, rel: str | None = None
    ) -> tuple[bytes, dict[str, str]]:
        resource = self.call_tool(name="resource.resolve", arguments={"project_id": project_id, "resource_id": resource_id})
        if rel:
            # A file referenced by the resource — e.g. a report's relative
            # figure link. Submitted figures (captured at associate, keyed by
            # the report's pinned version) serve from the blob store; only
            # un-submitted links fall back to a repo-jailed live read, a
            # local-mode convenience.
            blob = self._submitted_figure(project_id=project_id, resource=resource, rel=rel)
            if blob is not None:
                data, name = blob
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                return data, {
                    "Content-Type": mime,
                    "Content-Disposition": f'inline; filename="{name}"',
                }
            path = self._resource_path(resource=resource)
            path = (path.parent / rel).resolve()
            try:
                path.relative_to(self.app.workspace.repo_root)
            except ValueError as exc:
                raise ValidationError("relative file path escapes repo root") from exc
            if not path.is_file():
                raise NotFoundError(f"file not found next to resource: {rel}")
        else:
            path = self._resource_path(resource=resource)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path.read_bytes(), {
            "Content-Type": mime,
            "Content-Disposition": f'inline; filename="{path.name}"',
        }

    def _submitted_figure(
        self, *, project_id: str, resource: dict[str, Any], rel: str
    ) -> tuple[bytes, str] | None:
        version_id = self._latest_gated_version_id(resource=resource)
        if version_id is None:
            return None
        conn = self.app.store.connect()
        try:
            row = conn.execute(
                "SELECT sha256 FROM report_figures WHERE report_version_id = ? AND link_path = ?",
                (version_id, rel),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        try:
            data = self.app.blobs.get(namespace=project_id, sha256=str(row["sha256"]))
        except NotFoundError:
            return None
        return data, rel.rsplit("/", 1)[-1]

    def create_project(self, body: dict[str, Any]) -> dict[str, Any]:
        name = body.get("name") or body.get("title") or "Untitled Project"
        summary = body.get("summary") or body.get("description") or body.get("research_goal") or ""
        return self.call_tool(name="project.create", arguments={"name": name, "summary": summary})

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

    def sandbox_get_view(self, *, project_id: str, experiment_id: str) -> dict[str, Any]:
        row = self.app.sandboxes.get_row(experiment_id=experiment_id, project_id=project_id)
        if row is None:
            return {"experiment_id": experiment_id, "status": "none", "sandbox": None}
        return self.app.sandboxes.row_view(row=row)

    def sandbox_list_view(self, *, project_id: str) -> dict[str, Any]:
        return {
            "sandboxes": [
                self.app.sandboxes.row_view(row=row)
                for row in self.app.sandboxes.rows(project_id=project_id)
            ]
        }

    def sandbox_metrics_view(self, *, project_id: str, experiment_id: str) -> dict[str, Any]:
        return self.app.sandboxes.sample_metrics(experiment_id=experiment_id, project_id=project_id)

    def results_metrics_view(self, *, project_id: str, experiment_id: str) -> dict[str, Any]:
        """Archived MLflow metrics — durable results that outlive the sandbox VM."""
        return self.app.sandboxes.results_metrics(experiment_id=experiment_id, project_id=project_id)

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
        return build_experiment_figure(
            experiment=experiment,
            review_attempts=review_attempts,
            open_review_requests=self.app.reviews.open_requests_for_target(
                project_id=project_id, experiment_id=experiment_id
            ),
            sandbox=sandbox,
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
        chosen = _latest_graph_resource(
            resources=experiment.get("resources", []), attempt=attempt
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
            "ref_index": self._resolve_graph_refs(project_id=project_id, graph=graph),
        }

    def syntheses_view(self, *, project_id: str) -> dict[str, Any]:
        """All reflection waves plus the staleness/coverage signal for the UI."""
        syntheses = self.app.syntheses.list_syntheses(project_id=project_id)["syntheses"]
        conn = self.app.store.connect()
        try:
            signal = self.app.syntheses.reflection_signal(project_id=project_id, conn=conn)
            open_wave = self.app.syntheses.open_synthesis(conn=conn, project_id=project_id)
            published = self.app.syntheses.latest_published(conn=conn, project_id=project_id)
        finally:
            conn.close()
        return {
            "syntheses": syntheses,
            "current": open_wave or published,
            "open_synthesis": open_wave,
            "latest_published": published,
            "signal": signal,
        }

    def synthesis_detail(self, *, project_id: str, synthesis_id: str) -> dict[str, Any]:
        return self.app.syntheses.get_state(synthesis_id=synthesis_id, project_id=project_id)

    def project_logic_graph(self, *, project_id: str) -> dict[str, Any]:
        """The living project logic graph (role 'graph' on a synthesis).

        Chooses the open wave's graph when one exists (so the user can watch
        the synthesis being written), else the latest published one. The same
        payload shape as the per-experiment graph endpoint so the UI renders
        both through one component.
        """
        conn = self.app.store.connect()
        try:
            signal = self.app.syntheses.reflection_signal(project_id=project_id, conn=conn)
            synthesis = self.app.syntheses.open_synthesis(conn=conn, project_id=project_id)
            if synthesis is None or not self._synthesis_graph_resource(synthesis=synthesis):
                published = self.app.syntheses.latest_published(conn=conn, project_id=project_id)
                if published is not None and self._synthesis_graph_resource(synthesis=published):
                    synthesis = published
        finally:
            conn.close()
        base: dict[str, Any] = {
            "max_nodes": MAX_GRAPH_NODES,
            "signal": signal,
        }
        chosen = self._synthesis_graph_resource(synthesis=synthesis) if synthesis else None
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
                "problems": ["graph has no submitted content — re-associate it (role 'graph')"],
                "path": chosen.get("path"),
            }
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
            "ref_index": self._resolve_graph_refs(project_id=project_id, graph=graph),
        }

    def _association_pinned_text(self, resource: dict[str, Any]) -> str | None:
        """The submitted bytes behind a resources_for_target row (associated
        version → blob), or None when nothing was submitted."""
        version_id = resource.get("association_version_id")
        if not version_id:
            return None
        conn = self.app.store.connect()
        try:
            row = conn.execute(
                "SELECT project_id, content_sha256 FROM resource_versions WHERE id = ?",
                (str(version_id),),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        try:
            data = self.app.blobs.get(
                namespace=str(row["project_id"]), sha256=str(row["content_sha256"])
            )
        except NotFoundError:
            return None
        return data.decode("utf-8", errors="replace")

    def _synthesis_graph_resource(
        self, *, synthesis: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """The synthesis's graph association — current attempt preferred, with
        the prior-attempt fallback the experiment endpoint also uses."""
        if synthesis is None:
            return None
        return _latest_graph_resource(
            resources=synthesis.get("resources", []),
            attempt=synthesis.get("attempt_index"),
        )

    def _resolve_graph_refs(
        self, *, project_id: str, graph: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Resolve node ``refs`` strings to project records, on the read path.

        Refs are plain strings the agent wrote into its own file — repo-relative
        resource paths or known record ids (res_/rev_/claim_/exp_). Resolution
        is best-effort and read-only: an unresolvable ref degrades to
        ``{resolved: false}``, never an error, and nothing is enforced — whether
        to ref anything is the agent's editorial call. Keyed by ref string so
        the graph payload itself stays the file's exact content.
        """
        refs: list[str] = []
        seen: set[str] = set()
        for node in (graph or {}).get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_refs = node.get("refs")
            if not isinstance(node_refs, list):
                continue
            for ref in node_refs:
                if isinstance(ref, str) and ref.strip() and ref not in seen:
                    seen.add(ref)
                    refs.append(ref)
        if not refs:
            return {}
        conn = self.app.store.connect()
        try:
            return {
                ref: self._resolve_one_graph_ref(conn=conn, project_id=project_id, ref=ref)
                for ref in refs
            }
        finally:
            conn.close()

    def _resolve_one_graph_ref(self, *, conn, project_id: str, ref: str) -> dict[str, Any]:
        if ref.startswith("res_"):
            row = conn.execute(
                "SELECT id, path, kind, title, missing FROM resources"
                " WHERE id = ? AND project_id = ? AND deleted = 0",
                (ref, project_id),
            ).fetchone()
            if row:
                return self._graph_ref_resource(row=row)
        elif ref.startswith("rev_"):
            row = conn.execute(
                "SELECT id, role, verdict, created_at FROM reviews"
                " WHERE id = ? AND project_id = ?",
                (ref, project_id),
            ).fetchone()
            if row:
                return {
                    "type": "review",
                    "resolved": True,
                    "review_id": row["id"],
                    "role": row["role"],
                    "verdict": row["verdict"],
                    "created_at": row["created_at"],
                }
        elif ref.startswith("claim_"):
            row = conn.execute(
                "SELECT id, statement, status FROM claims WHERE id = ? AND project_id = ?",
                (ref, project_id),
            ).fetchone()
            if row:
                return {
                    "type": "claim",
                    "resolved": True,
                    "claim_id": row["id"],
                    "statement": row["statement"],
                    "status": row["status"],
                }
        elif ref.startswith("exp_"):
            row = conn.execute(
                "SELECT id, intent, status FROM experiments WHERE id = ? AND project_id = ?",
                (ref, project_id),
            ).fetchone()
            if row:
                return {
                    "type": "experiment",
                    "resolved": True,
                    "experiment_id": row["id"],
                    "intent": row["intent"],
                    "status": row["status"],
                }
        elif ref.startswith("syn_"):
            row = conn.execute(
                "SELECT id, title, status, published_at FROM syntheses WHERE id = ? AND project_id = ?",
                (ref, project_id),
            ).fetchone()
            if row:
                return {
                    "type": "synthesis",
                    "resolved": True,
                    "synthesis_id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "published_at": row["published_at"],
                }
        else:
            row = conn.execute(
                "SELECT id, path, kind, title, missing FROM resources"
                " WHERE project_id = ? AND path = ? AND deleted = 0",
                (project_id, ref),
            ).fetchone()
            if row:
                return self._graph_ref_resource(row=row)
            # Path refs resolve against registered resources only — the record
            # the control plane can see. (The old working-tree existence probe
            # was a local-only signal; cloud mode has no disk to probe.)
            return {
                "type": "unknown",
                "resolved": False,
                "hint": "not a registered resource path; register the file to make this ref resolvable",
            }
        return {"type": "unknown", "resolved": False}

    def _graph_ref_resource(self, *, row) -> dict[str, Any]:
        return {
            "type": "resource",
            "resolved": True,
            "resource_id": row["id"],
            "path": row["path"],
            "kind": row["kind"],
            "title": row["title"],
            "missing": bool(row["missing"]),
        }

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
    auth: AuthService | None = None,
    allowed_origins: list[str] | None = None,
    task_queue: Any | None = None,
    sync_targets_source: Any | None = None,
) -> FastAPI:
    # Auth seam (cloud plan Phase 7). ``auth=None`` is local mode: auth OFF, the
    # implicit LOCAL_PRINCIPAL on every request, CORS wide open, loopback bind
    # enforced by http_server — byte-identical to before this phase. An injected
    # AuthService flips on mandatory bearer auth and origin-restricted CORS
    # (control mode). Every existing caller passes neither, so nothing changes.
    if (app is None) == (router is None):
        raise ValueError("provide exactly one of app or router")
    api = ResearchHttpApi(app=app) if app is not None else None
    auth_required = auth is not None

    def api_for_project(project_id: str) -> ResearchHttpApi:
        if router is not None:
            return ResearchHttpApi(app=router.app_for_project(project_id))
        assert api is not None
        return api

    def default_api() -> ResearchHttpApi | None:
        if api is not None:
            return api
        assert router is not None
        app = router.any_app()
        return ResearchHttpApi(app=app) if app is not None else None

    def route_call_tool(
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        activity_source: str = "http",
    ) -> dict[str, Any]:
        if router is not None:
            return router.call_tool(
                name=name,
                arguments=arguments,
                context=context,
                activity_source=activity_source,
            )
        assert api is not None
        arguments = dict(arguments or {})
        if name in PROJECT_SCOPED_TOOL_NAMES and "project_id" not in arguments and (context or {}).get("repo_root"):
            projects = api.app.projects.list_projects()["projects"]
            if len(projects) != 1:
                raise ValidationError(
                    "project_id is required when the repo has multiple projects",
                    details={"projects": [project["id"] for project in projects]},
                )
            arguments["project_id"] = projects[0]["id"]
        return api.app.call_tool(name=name, arguments=arguments, activity_source=activity_source)

    http = FastAPI(title="Research Plugin API", version=__version__)
    # CORS (cloud plan Phase 7): local mode keeps the wide-open `*` policy
    # (loopback-only, auth off) — unchanged. Control mode uses an explicit
    # allowed-origins list (empty by default until the hosted UI origin is
    # configured) and allows the Authorization header so the SPA can send a
    # bearer token.
    if auth_required:
        http.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins or [],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Accept", "Authorization"],
        )
    else:
        http.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Accept"],
        )

    @http.middleware("http")
    async def attach_principal(request: Request, call_next):
        # Principal middleware (cloud plan Phase 7). Auth off (local mode): the
        # implicit LOCAL_PRINCIPAL is attached and behavior is unchanged —
        # loopback bind already enforced by http_server. Auth on (control
        # mode): a valid `Authorization: Bearer` is required; missing/invalid/
        # expired/revoked returns 401 before any handler runs. CORS preflights
        # (OPTIONS) are never authenticated.
        if not auth_required:
            request.state.principal = LOCAL_PRINCIPAL
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        # /health is unauthenticated liveness (load balancers probe it before a
        # token exists); it returns a slim, path-free shape in control mode, so
        # no host detail leaks despite being open.
        if request.url.path == "/health":
            return await call_next(request)
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header[:7].lower() == "bearer " else None
        try:
            assert auth is not None
            request.state.principal = auth.resolve(token=token)
        except AuthError as exc:
            return JSONResponse(
                {"detail": str(exc), "error_code": "unauthorized"},
                status_code=401,
            )
        return await call_next(request)

    @http.middleware("http")
    async def log_http_activity(request: Request, call_next):
        started = monotonic_ms()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            path = str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")
            if api is not None:
                api.app.activity.http_request(
                    method=request.method,
                    path=path,
                    status=status,
                    duration_ms=monotonic_ms() - started,
                )

    @http.exception_handler(ResearchPluginError)
    async def research_error_handler(_request: Request, exc: ResearchPluginError) -> JSONResponse:
        status = 404 if isinstance(exc, NotFoundError) else 400
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
        if auth_required:
            return {"ok": True, "version": __version__}
        if router is not None:
            return {"ok": True, "version": __version__, **router.health()}
        assert api is not None
        return api.health()

    @http.get("/api/activity")
    def activity(limit: int = Query(100, ge=1), source: str | None = None) -> dict[str, Any]:
        if router is not None:
            return router.activity_recent(limit=limit, source=source)
        assert api is not None
        return api.activity(limit=limit, source=source)

    # /api/debug/* expose tool-call internals. In control mode the principal
    # middleware above already requires a valid bearer on every route, so these
    # are principal-gated with no per-route change (cloud plan Phase 7); local
    # mode keeps them open on loopback, unchanged.
    @http.get("/api/debug/tool-calls")
    def tool_call_stats(
        minutes: int | None = Query(None, ge=1),
        source: str | None = None,
        status: str | None = None,
        tool: str | None = None,
        limit: int = Query(200, ge=1, le=2000),
        sort: str = "ts",
        order: str = "desc",
    ) -> dict[str, Any]:
        target = default_api()
        if target is None:
            # Mirror ToolCallStore.stats' empty `base` shape so the Debug UI
            # renders the same whether the store is empty or no app exists yet.
            return {
                "calls": [],
                "by_tool": [],
                "totals": {"calls": 0, "sent_chars": 0, "received_chars": 0, "error_calls": 0},
                "coverage": {"calls": 0, "stored": 0, "oldest_ts": None, "newest_ts": None, "capped": False},
                "filter": {"minutes": minutes, "source": source, "status": status, "tool": tool},
            }
        return target.tool_call_stats(
            minutes=minutes, source=source, status=status, tool=tool,
            limit=limit, sort=sort, order=order,
        )

    @http.get("/api/debug/tool-calls/{call_id}")
    def tool_call_detail(call_id: int) -> dict[str, Any]:
        target = default_api()
        if target is None:
            raise NotFoundError("no project instantiated yet")
        return target.tool_call_detail(call_id=call_id)

    @http.post("/api/debug/tool-calls/clear")
    def tool_calls_clear() -> dict[str, Any]:
        target = default_api()
        if target is None:
            return {"cleared": 0}
        return target.tool_calls_clear()

    @http.get("/api/projects")
    def list_projects() -> dict[str, Any]:
        if router is not None:
            return router.list_projects()
        assert api is not None
        return api.call_tool(name="project.list", arguments={})

    @http.post("/api/projects", status_code=201)
    def create_project(body: JsonBody = Body(default=None)) -> dict[str, Any]:
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
        return api.create_project(body=payload)

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
        return api_for_project(project_id).app.workflow.status_and_next(project_id=project_id, experiment_id=experiment_id)

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
        return api_for_project(project_id).app.workflow.status_and_next(project_id=project_id, experiment_id=experiment_id)

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
        return api_for_project(project_id).call_tool(name="resource.associate", arguments={"project_id": project_id, "resource_id": resource_id, **(body or {})})

    @http.delete("/api/projects/{project_id}/resources/{resource_id}")
    def delete_resource(project_id: str, resource_id: str) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(name="resource.delete", arguments={"project_id": project_id, "resource_id": resource_id})

    @http.get("/api/projects/{project_id}/resources/{resource_id}/content")
    def resource_content(project_id: str, resource_id: str) -> dict[str, Any]:
        return api_for_project(project_id).resource_content(project_id=project_id, resource_id=resource_id)

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
    def start_review(project_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
        return api_for_project(project_id).start_review(project_id=project_id, body=body or {})

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
    def get_sandbox(project_id: str, experiment_id: str) -> dict[str, Any]:
        return api_for_project(project_id).sandbox_get_view(project_id=project_id, experiment_id=experiment_id)

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/sandbox/metrics")
    def sandbox_metrics(project_id: str, experiment_id: str) -> dict[str, Any]:
        return api_for_project(project_id).sandbox_metrics_view(project_id=project_id, experiment_id=experiment_id)

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/results/metrics")
    def experiment_results_metrics(project_id: str, experiment_id: str) -> dict[str, Any]:
        return api_for_project(project_id).results_metrics_view(
            project_id=project_id, experiment_id=experiment_id
        )

    @http.get("/api/projects/{project_id}/experiments/{experiment_id}/sandbox/terminal")
    def sandbox_terminal(
        project_id: str,
        experiment_id: str,
        tail: int | None = None,
        since: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"project_id": project_id, "experiment_id": experiment_id}
        if tail is not None:
            args["tail"] = tail
        if since is not None:
            args["since"] = since
        return api_for_project(project_id).call_tool(name="sandbox.terminal", arguments=args)

    @http.post("/api/projects/{project_id}/experiments/{experiment_id}/sandbox/sync")
    def sync_sandbox(project_id: str, experiment_id: str) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(
            name="sandbox.sync",
            arguments={"project_id": project_id, "experiment_id": experiment_id},
        )

    @http.post("/api/projects/{project_id}/experiments/{experiment_id}/sandbox/release")
    def release_sandbox(project_id: str, experiment_id: str) -> dict[str, Any]:
        return api_for_project(project_id).call_tool(
            name="sandbox.release",
            arguments={"project_id": project_id, "experiment_id": experiment_id},
        )

    @http.get("/api/projects/{project_id}/events")
    def events(project_id: str, limit: int = Query(100, ge=1)) -> dict[str, Any]:
        return api_for_project(project_id).events(project_id=project_id, limit=limit)

    # MCP-shaped endpoints — drive the same ResearchPluginApp.call_tool path that
    # the stdio MCP server uses. The stdio MCP proxy forwards Codex tool calls
    # here; UIs talk to /api/* and don't need these. Errors are mapped to the
    # ResearchPluginError handler above so they preserve error_code + details.
    @http.get("/mcp/tools")
    def mcp_tools_list() -> dict[str, Any]:
        if router is not None:
            return {"tools": router.list_tools()}
        assert api is not None
        return {"tools": api.app.list_tools()}

    @http.post("/mcp/call")
    def mcp_call(body: JsonBody = Body(default=None)) -> dict[str, Any]:
        payload = body or {}
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("tool name is required", details={"field": "name"})
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValidationError("arguments must be an object", details={"field": "arguments"})
        context = payload.get("context") or {}
        if context is not None and not isinstance(context, dict):
            raise ValidationError("context must be an object", details={"field": "context"})
        result = route_call_tool(
            name=name,
            arguments=arguments,
            context=context,
            activity_source="mcp",
        )
        return {"result": result}

    # ---- daemon task channel + sync-target poll (cloud plan Phase 8) ----
    # Daemon-initiated only: the cloud NEVER connects inbound. These endpoints
    # exist only on the control composition (task_queue/sync_targets_source
    # injected); local mode's in-process channel never mounts them. They are
    # principal-gated by the middleware above when auth is on (control mode).
    if task_queue is not None:
        @http.get("/api/daemon/tasks")
        def daemon_poll_tasks(
            client_id: str = Query(""),  # noqa: ARG001 — v1 single daemon per tenant
            wait: int = Query(25, ge=0, le=60),
        ) -> dict[str, Any]:
            # Long-poll for the next data-plane task; {"task": null} on timeout.
            task = task_queue.poll(wait_seconds=float(wait))
            return {"task": task}

        @http.post("/api/daemon/tasks/{task_id}/ack")
        def daemon_ack_task(task_id: str, body: JsonBody = Body(default=None)) -> dict[str, Any]:
            payload = body or {}
            task_queue.ack(
                task_id=task_id,
                ok=bool(payload.get("ok")),
                result=payload.get("result"),
                error=payload.get("error"),
            )
            return {"acked": True}

    if sync_targets_source is not None:
        @http.get("/api/daemon/sync-targets")
        def daemon_sync_targets(client_id: str = Query("")) -> dict[str, Any]:  # noqa: ARG001
            # "My running sandboxes + a lease-backed session for each" — the
            # exact InProcessControlPlaneView call, now an HTTP poll. The
            # session carries SSH endpoint + remote dirs + lease; no
            # machine-local path (the daemon enriches its own key paths).
            targets = sync_targets_source.sync_targets()
            return {
                "targets": [
                    {
                        "experiment_id": str(t["row"].get("experiment_id") or ""),
                        "session": t["session"],
                    }
                    for t in targets
                ]
            }

    return http
