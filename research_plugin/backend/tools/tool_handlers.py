"""Tool-name registry over composed service objects."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..mlflow import (
    METRICS_EXHIBIT_FILENAME,
    MLFLOW_TERMINAL_RUN_STATUSES,
    mlflow_experiment_name,
    mlflow_visible_for_status,
)
from ..services.experiment_views import slim_experiment_state
from ..utils import ValidationError
from .exhibits import (
    exhibit_rel_path,
    finalize_metrics_exhibit,
    preview_metrics_exhibit,
)

# Terminal experiment statuses that are story-worthy enough to carry a
# feed_note (see _attach_feed_note): a completed, failed, or abandoned/killed
# run. Kept local to the surface layer rather than imported from
# domain.workflow_gates.TERMINAL_STATUSES — the event phrasing is a
# surface-level presentation concern, not a workflow-gate one.
_TERMINAL_EXPERIMENT_FEED_EVENTS = {
    "complete": "experiment_complete",
    "failed": "experiment_failed",
    "abandoned": "experiment_abandoned",
}


def _attach_feed_note(
    result: dict[str, Any],
    *,
    feed: Any,
    project_id: str,
    entity_id: str,
    event: str,
) -> None:
    """Attach an optional ``feed_note`` advisory to ``result`` in place.

    Never raises: a feed hiccup (a bad connection, a schema surprise, ...)
    must not break the workflow transition, review check, or MLflow call
    whose response this rides on. Absent rather than null when there is
    nothing to say, matching how every other optional response field in this
    module (``mlflow``, ``metrics_exhibit``, ...) is attached.
    """
    try:
        note = feed.feed_note_for(
            project_id=project_id, entity_id=entity_id, event=event
        )
    except Exception:  # noqa: BLE001 - advisory only, must never block
        note = None
    if note is not None:
        result["feed_note"] = note


def _experiment_get_state_agent(
    *,
    experiments: Any,
    mlflow_tracking: Any,
    experiment_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    full = experiments.get_state(experiment_id=experiment_id, project_id=project_id)
    slim = slim_experiment_state(full)
    return _with_mlflow_if_visible(
        state=slim,
        mlflow_tracking=mlflow_tracking,
        project_id=str(full.get("project_id") or project_id or ""),
        experiment_id=experiment_id,
    )


def _experiment_list_agent(
    *, experiments: Any, project_id: str | None = None
) -> dict[str, Any]:
    full = experiments.list_experiments(project_id=project_id)
    return {
        "experiments": [
            slim_experiment_state(experiment) for experiment in full["experiments"]
        ]
    }


def _mlflow_connection(
    *, mlflow_tracking: Any, project_id: str, experiment_id: str
) -> dict[str, Any]:
    """Central MLflow connection block for one experiment — the same context
    tracking URI, rp/<project>/<experiment> name, and env vars surfaced so any
    run location can connect the same way."""
    if mlflow_tracking is None or not project_id or not experiment_id:
        return {"configured": False}
    return mlflow_tracking.context(project_id=project_id, experiment_id=experiment_id).to_dict()


def _attach_mlflow_run(block: dict[str, Any], run: dict[str, Any] | None) -> dict[str, Any]:
    if not run:
        return block
    out = dict(block)
    out["run"] = run
    run_id = str(run.get("run_id") or "")
    if run_id:
        env = dict(out.get("env") or {})
        env["MLFLOW_RUN_ID"] = run_id
        env["RP_MLFLOW_RUN_ID"] = run_id
        out["env"] = env
    return out


def _mlflow_project_connection(
    *, mlflow_tracking: Any, project_id: str, experiments: Any
) -> dict[str, Any]:
    """Project-level MLflow connection and namespace map for direct API reads."""
    if mlflow_tracking is None or not project_id:
        return {"configured": False}
    block = dict(mlflow_tracking.project_context(project_id=project_id))
    listed = experiments.list_experiments(project_id=project_id)["experiments"]
    block["experiments"] = [
        {
            "experiment_id": exp.get("id"),
            "name": exp.get("name") or exp.get("id"),
            "status": exp.get("status") or "",
            "intent": exp.get("intent") or "",
            "mlflow_experiment_name": mlflow_experiment_name(
                project_id=project_id, experiment_id=str(exp.get("id") or "")
            ),
        }
        for exp in listed
        if exp.get("id")
    ]
    return block


def _mlflow_guidance(block: dict[str, Any]) -> str:
    if not block.get("configured"):
        note = str(block.get("note") or "").strip()
        if note:
            return note
        return (
            "If you run a quantitative experiment, log it to MLflow — but no "
            "central MLflow tracking URI is configured on this backend yet."
        )
    if block.get("experiment_name"):
        return (
            "For this quantitative experiment, set the variables in mlflow.env "
            "(MLFLOW_TRACKING_URI, MLFLOW_EXPERIMENT_NAME, …), then log params, "
            "metrics, artifacts, and required tags to the centralized server. "
            "Use MLflow's native APIs for reads and comparisons."
        )
    return (
        "Use MLflow's native APIs with mlflow.env.MLFLOW_TRACKING_URI to browse "
        "quantitative runs. Search experiment names under "
        f"{block.get('experiment_namespace_prefix') or 'the project namespace'} "
        "or use mlflow.experiments as the plugin experiment-to-MLflow-name map."
    )


def _mlflow_context_response(
    *,
    project_id: str,
    experiment_id: str | None,
    mlflow: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "project_id": project_id,
        "scope": "experiment" if experiment_id else "project",
        "mlflow": mlflow,
        "guidance": _mlflow_guidance(mlflow),
    }
    if experiment_id:
        result["experiment_id"] = experiment_id
    return result


def _with_mlflow_if_visible(
    *,
    state: dict[str, Any],
    mlflow_tracking: Any,
    project_id: str,
    experiment_id: str,
) -> dict[str, Any]:
    if not mlflow_visible_for_status(state.get("status")):
        return state
    block = _mlflow_connection(
        mlflow_tracking=mlflow_tracking,
        project_id=project_id,
        experiment_id=experiment_id,
    )
    block = _attach_mlflow_run(block, state.get("mlflow_run"))
    state["mlflow"] = block
    state["mlflow_guidance"] = _mlflow_guidance(block)
    return state


def _create_and_record_mlflow_run(
    *,
    experiments: Any,
    mlflow_tracking: Any,
    state: dict[str, Any],
    project_id: str,
    experiment_id: str,
) -> dict[str, Any]:
    attempt_index = int(state.get("attempt_index") or 1)
    run = mlflow_tracking.create_run(
        project_id=project_id,
        experiment_id=experiment_id,
        attempt_index=attempt_index,
        run_name=f"{experiment_id}-attempt-{attempt_index}",
    )
    if not (run.get("run_id") or run.get("error")):
        return state
    return experiments.record_mlflow_run(
        project_id=project_id,
        experiment_id=experiment_id,
        run=run,
    )


def _ensure_mlflow_run_for_start(
    *,
    experiments: Any,
    mlflow_tracking: Any,
    state: dict[str, Any],
    project_id: str,
    experiment_id: str,
) -> dict[str, Any]:
    if mlflow_tracking is None:
        return state
    existing = state.get("mlflow_run") or {}
    if existing.get("run_id"):
        return state
    return _create_and_record_mlflow_run(
        experiments=experiments,
        mlflow_tracking=mlflow_tracking,
        state=state,
        project_id=project_id,
        experiment_id=experiment_id,
    )


def _ensure_mlflow_run_for_retry(
    *,
    experiments: Any,
    mlflow_tracking: Any,
    state: dict[str, Any],
    project_id: str,
    experiment_id: str,
) -> dict[str, Any]:
    """An infra retry resumes the persisted run while it is still open. Once
    that run was finalized (e.g. FAILED after the crash), resuming would log
    into a closed run — mint a fresh run for the same attempt instead. A
    retry with no persisted run (creation failed at start_running) is also
    the natural backfill point."""
    if mlflow_tracking is None:
        return state
    existing = state.get("mlflow_run") or {}
    persisted_status = str(existing.get("status") or "").upper()
    if existing.get("run_id") and persisted_status not in MLFLOW_TERMINAL_RUN_STATUSES:
        return state
    return _create_and_record_mlflow_run(
        experiments=experiments,
        mlflow_tracking=mlflow_tracking,
        state=state,
        project_id=project_id,
        experiment_id=experiment_id,
    )


def _exhibit_expectation(*, experiment_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Expectation-setting handed to the agent the moment execution starts:
    whatever it logs IS the record, so proper logging is the only way its
    numbers make it into the review."""
    path = exhibit_rel_path(
        experiment_id=experiment_id, name=str(state.get("name") or "")
    )
    return {
        "final_path": path,
        "preview_tool": "experiment.exhibit",
        "notice": (
            "At submit_results the system generates a metrics exhibit from "
            "your MLflow runs (every run in this attempt's window — no "
            "curation) and pulled result files (metrics.json, results/*.json "
            "associated with role 'result'), and pins it at "
            f"{path}. Your report must reference {METRICS_EXHIBIT_FILENAME} "
            "and answer around it — track accordingly: log every run to the "
            "MLflow env you were handed, tag project_id/experiment_id, and "
            "pull result files before submitting. Preview anytime with "
            "experiment.exhibit; runs logged after submit_results do not "
            "exist for this attempt."
        ),
    }


def build_control_tool_handlers(
    *,
    workflow: Any,
    projects: Any,
    project_overview: Any,
    claims: Any,
    experiments: Any,
    reflections: Any,
    resources: Any,
    storage: Any | None,
    reviews: Any,
    sandboxes: Any,
    mlflow_tracking: Any,
    feed: Any,
    research_map: Any,
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Map control-plane tool names to service methods.

    This is intentionally a thin registry: composition supplies the services,
    and ToolDispatcher verifies the final name set against TOOL_CONTRACTS.
    """
    def experiment_get_state_agent(
        *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        return _experiment_get_state_agent(
            experiments=experiments,
            mlflow_tracking=mlflow_tracking,
            experiment_id=experiment_id,
            project_id=project_id,
        )

    def experiment_list_agent(
        *, project_id: str | None = None
    ) -> dict[str, Any]:
        return _experiment_list_agent(experiments=experiments, project_id=project_id)

    def experiment_transition_agent(
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        exhibit = None
        if transition == "submit_results":
            # Finalize the system metrics exhibit BEFORE the gate runs: the
            # report validator requires a reference to the pinned exhibit, and
            # pinning at this moment closes the late-write window — runs
            # logged after submit_results do not exist for this attempt.
            state = experiments.get_state(
                experiment_id=experiment_id, project_id=project_id
            )
            if str(state.get("status")) == "running":
                exhibit = finalize_metrics_exhibit(
                    experiments=experiments,
                    resources=resources,
                    mlflow_tracking=mlflow_tracking,
                    state=state,
                )
        result = experiments.transition(
            experiment_id=experiment_id,
            transition=transition,
            evidence=evidence,
            project_id=project_id,
        )
        resolved_project_id = str(result.get("project_id") or project_id or "")
        if transition == "start_running":
            result = _ensure_mlflow_run_for_start(
                experiments=experiments,
                mlflow_tracking=mlflow_tracking,
                state=result,
                project_id=resolved_project_id,
                experiment_id=experiment_id,
            )
        elif transition == "retry_running":
            result = _ensure_mlflow_run_for_retry(
                experiments=experiments,
                mlflow_tracking=mlflow_tracking,
                state=result,
                project_id=resolved_project_id,
                experiment_id=experiment_id,
            )
        slim = slim_experiment_state(result)
        # The moment an experiment starts running, hand the agent the MLflow
        # connection block so a quantitative run — including a local, non-sandbox
        # one — can log to the centralized server without hunting for the URI.
        if mlflow_visible_for_status(slim.get("status")):
            slim = _with_mlflow_if_visible(
                state=slim,
                mlflow_tracking=mlflow_tracking,
                project_id=resolved_project_id,
                experiment_id=experiment_id,
            )
        if transition in ("start_running", "retry_running"):
            slim["metrics_exhibit"] = _exhibit_expectation(
                experiment_id=experiment_id, state=slim
            )
        elif transition == "submit_results" and exhibit is not None:
            slim["metrics_exhibit"] = {
                "pinned": True,
                "path": exhibit_rel_path(
                    experiment_id=experiment_id, name=str(slim.get("name") or "")
                ),
                "verdict": exhibit["verdict"],
            }
        terminal_event = _TERMINAL_EXPERIMENT_FEED_EVENTS.get(str(slim.get("status")))
        if terminal_event is not None:
            _attach_feed_note(
                slim,
                feed=feed,
                project_id=resolved_project_id,
                entity_id=experiment_id,
                event=terminal_event,
            )
        return slim

    def experiment_exhibit_agent(
        *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        return preview_metrics_exhibit(
            experiments=experiments,
            resources=resources,
            mlflow_tracking=mlflow_tracking,
            experiment_id=experiment_id,
            project_id=project_id,
        )

    def mlflow_context_agent(
        *, project_id: str, experiment_id: str | None = None
    ) -> dict[str, Any]:
        if not experiment_id:
            block = _mlflow_project_connection(
                mlflow_tracking=mlflow_tracking,
                project_id=project_id,
                experiments=experiments,
            )
            return _mlflow_context_response(
                project_id=project_id,
                experiment_id=None,
                mlflow=block,
            )
        state = experiments.get_state(experiment_id=experiment_id, project_id=project_id)
        resolved_project_id = str(state.get("project_id") or project_id or "")
        block = _mlflow_connection(
            mlflow_tracking=mlflow_tracking,
            project_id=resolved_project_id,
            experiment_id=experiment_id,
        )
        block = _attach_mlflow_run(block, state.get("mlflow_run"))
        return _mlflow_context_response(
            project_id=resolved_project_id,
            experiment_id=experiment_id,
            mlflow=block,
        )

    def mlflow_finalize_run_agent(
        *,
        project_id: str,
        experiment_id: str,
        run_id: str | None = None,
        status: str | None = "FINISHED",
        wait_seconds: float = 2.0,
    ) -> dict[str, Any]:
        state = experiments.get_state(experiment_id=experiment_id, project_id=project_id)
        resolved_project_id = str(state.get("project_id") or project_id or "")
        existing_run = state.get("mlflow_run") or {}
        resolved_run_id = str(run_id or existing_run.get("run_id") or "")
        if mlflow_tracking is None:
            return {
                "project_id": resolved_project_id,
                "experiment_id": experiment_id,
                "configured": False,
                "run_id": resolved_run_id,
                "error": "MLflow tracking is not configured on this backend.",
            }
        result = mlflow_tracking.finalize_run(
            project_id=resolved_project_id,
            experiment_id=experiment_id,
            run_id=resolved_run_id,
            status=status,
            wait_seconds=wait_seconds,
        )
        run = result.get("run")
        refreshed_state = state
        persisted_run_id = str(existing_run.get("run_id") or "")
        # Only refresh the experiment's canonical run block for the run it
        # actually owns — finalizing an explicit foreign run_id must not
        # repoint the persisted identity.
        if (
            isinstance(run, dict)
            and run.get("run_id")
            and (not persisted_run_id or str(run.get("run_id")) == persisted_run_id)
        ):
            refreshed_state = experiments.record_mlflow_run(
                project_id=resolved_project_id,
                experiment_id=experiment_id,
                run=run,
                event_type="experiment.mlflow_run_refreshed",
            )
        slim = slim_experiment_state(refreshed_state)
        if mlflow_visible_for_status(slim.get("status")):
            slim = _with_mlflow_if_visible(
                state=slim,
                mlflow_tracking=mlflow_tracking,
                project_id=resolved_project_id,
                experiment_id=experiment_id,
            )
        out = dict(result)
        out["project_id"] = resolved_project_id
        out["experiment_id"] = experiment_id
        out["experiment"] = slim
        if isinstance(run, dict) and run.get("run_id"):
            _attach_feed_note(
                out,
                feed=feed,
                project_id=resolved_project_id,
                entity_id=experiment_id,
                event="mlflow_run_finalized",
            )
        return out

    def review_status_agent(
        *, target_type: str, target_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Wraps reviews.status so the PRODUCER side — not the reviewer, who
        already saw the verdict at review.submit — gets the feed_note. This
        is the tool workflow.status_and_next's review_gate tells the agent to
        poll (``allowed: ["review.status"]``) while a review is pending, so
        it is the one main-agent-visible read naturally keyed to "the
        experiment under review". Only fires once a verdict actually exists
        (a bare pending-request check has nothing story-worthy to say yet)."""
        result = reviews.status(
            target_type=target_type, target_id=target_id, project_id=project_id
        )
        if target_type == "experiment" and result.get("reviews"):
            try:
                resolved_project_id = str(
                    experiments.get_state(
                        experiment_id=target_id, project_id=project_id
                    ).get("project_id")
                    or project_id
                    or ""
                )
            except Exception:  # noqa: BLE001 - advisory only, must never block
                resolved_project_id = ""
            if resolved_project_id:
                _attach_feed_note(
                    result,
                    feed=feed,
                    project_id=resolved_project_id,
                    entity_id=target_id,
                    event="experiment_review_verdict",
                )
        return result

    def _map_snapshot_result(png: bytes, meta: dict[str, Any]) -> dict[str, Any]:
        """Tool-shaped snapshot: the PNG rides as base64 under the reserved
        key the stdio proxy converts into an MCP image content block (with a
        file-path fallback the agent can Read). Same renderer as the UI —
        the pixel-parity hard line."""
        import base64

        viewport = meta["viewport"]
        return {
            **meta,
            "image_png_base64": base64.b64encode(png).decode("ascii"),
            "media_type": "image/png",
            "guidance": (
                "Read the image; margin letters/numbers are grid cell refs — "
                "map.snapshot(cell='C4') zooms into a cell, or reuse this "
                f"viewport (cx={viewport['cx']:.0f}, cy={viewport['cy']:.0f}) "
                "with a higher zoom. Entity ids become readable at L3."
            ),
        }

    def map_overview_agent(
        *, project_id: str, w: int = 1200, h: int = 800
    ) -> dict[str, Any]:
        png, meta = research_map.snapshot(project_id=project_id, w=w, h=h)
        return _map_snapshot_result(png, meta)

    def map_snapshot_agent(
        *,
        project_id: str,
        cx: float | None = None,
        cy: float | None = None,
        zoom: float | None = None,
        cell: str | None = None,
        w: int = 1200,
        h: int = 800,
    ) -> dict[str, Any]:
        png, meta = research_map.snapshot(
            project_id=project_id, cx=cx, cy=cy, zoom=zoom, cell=cell, w=w, h=h
        )
        return _map_snapshot_result(png, meta)

    def map_locate_agent(
        *,
        project_id: str,
        entity_id: str,
        zoom: float = 2.2,
        w: int = 1200,
        h: int = 800,
    ) -> dict[str, Any]:
        png, meta = research_map.locate(
            project_id=project_id, entity_id=entity_id, zoom=zoom, w=w, h=h
        )
        return {**_map_snapshot_result(png, meta), "entity_id": entity_id}

    handlers = {
        "workflow.status_and_next": workflow.status_and_next_agent,
        "project.create": projects.create,
        "project.update": projects.update,
        "project.get": projects.get,
        "project.current": project_overview.current_project,
        "project.list": projects.list_projects,
        "claim.create": claims.create,
        "claim.list": claims.list_claims,
        "claim.update": claims.update,
        "experiment.create": experiments.create,
        "experiment.list": experiment_list_agent,
        "experiment.get_state": experiment_get_state_agent,
        "experiment.transition": experiment_transition_agent,
        "experiment.exhibit": experiment_exhibit_agent,
        "mlflow.context": mlflow_context_agent,
        "mlflow.finalize_run": mlflow_finalize_run_agent,
        "reflection.create": reflections.create,
        "reflection.get": reflections.get,
        "reflection.list": reflections.list,
        "reflection.transition": reflections.transition,
        "resource.delete": resources.delete,
        "resource.list": resources.list_resources,
        "resource.resolve": resources.resolve,
        "review.request": reviews.request,
        "review.start": reviews.start,
        "review.submit": reviews.submit,
        "review.status": review_status_agent,
        "sandbox.options": sandboxes.options,
        "sandbox.get": sandboxes.get,
        "sandbox.list": sandboxes.list_sandboxes,
        "sandbox.release": sandboxes.release,
        "sandbox.extend": sandboxes.extend,
        "sandbox.terminal": sandboxes.terminal,
        "sandbox.runs": sandboxes.runs,
        "sandbox.health": sandboxes.health,
        "feed.register": feed.register,
        "feed.list": feed.list_posts,
        "map.overview": map_overview_agent,
        "map.snapshot": map_snapshot_agent,
        "map.locate": map_locate_agent,
    }
    if storage is not None:
        handlers.update(
            {
                "storage.put_object": storage.put_object,
                "storage.complete_upload": storage.complete_upload,
                "storage.list": storage.list_objects,
                "storage.resolve": storage.resolve,
                "storage.pin": storage.pin,
                "storage.unpin": storage.unpin,
                "storage.renew": storage.renew,
                "storage.delete": storage.delete,
            }
        )
    return handlers


def build_local_tool_handlers(
    *,
    workflow: Any,
    projects: Any,
    project_overview: Any,
    claims: Any,
    experiments: Any,
    reflections: Any,
    resources: Any,
    storage: Any | None,
    reviews: Any,
    sandboxes: Any,
    mlflow_tracking: Any,
    feed: Any,
    research_map: Any,
    resource_register_file: Callable[..., dict[str, Any]],
    resource_validate: Callable[..., dict[str, Any]],
    experiment_materialize_folders: Callable[..., dict[str, Any]],
    # Data-plane local IO: required — there is no control-plane fallback.
    sandbox_pull_outputs: Callable[..., dict[str, Any]],
    resource_associate: Callable[..., dict[str, Any]] | None = None,
    feed_post: Callable[..., dict[str, Any]] | None = None,
    storage_upload_file: Callable[..., dict[str, Any]] | None = None,
    storage_download_file: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Map all local-mode tool names to service methods."""
    def resource_associate_batch(
        *, associations: list[dict[str, Any]], project_id: str | None = None
    ) -> dict[str, Any]:
        associate = (
            resource_associate if resource_associate is not None else resources.associate
        )
        applied = [
            associate(project_id=project_id, **dict(association))
            for association in associations
        ]
        return {"associations": applied, "count": len(applied)}

    handlers = build_control_tool_handlers(
        workflow=workflow,
        projects=projects,
        project_overview=project_overview,
        claims=claims,
        experiments=experiments,
        reflections=reflections,
        resources=resources,
        storage=storage,
        reviews=reviews,
        sandboxes=sandboxes,
        mlflow_tracking=mlflow_tracking,
        feed=feed,
        research_map=research_map,
    )
    handlers.update(
        {
            "resource.register_file": resource_register_file,
            "resource.validate": resource_validate,
            "resource.associate": (
                resource_associate if resource_associate is not None else resources.associate
            ),
            "resource.associate_batch": resource_associate_batch,
            "experiment.materialize_folders": experiment_materialize_folders,
            "sandbox.request": sandboxes.request,
            "sandbox.attach": sandboxes.attach,
            "sandbox.pull_outputs": sandbox_pull_outputs,
            "feed.post": feed_post if feed_post is not None else feed.post,
        }
    )
    if storage is not None:
        handlers.update(
            {
                "storage.upload_file": (
                    storage_upload_file
                    if storage_upload_file is not None
                    else storage.upload_file
                ),
                "storage.download_file": (
                    storage_download_file
                    if storage_download_file is not None
                    else storage.download_file
                ),
            }
        )
    return handlers
