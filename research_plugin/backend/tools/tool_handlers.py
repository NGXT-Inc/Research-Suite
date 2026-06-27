"""Tool-name registry over composed service objects."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..services.experiment_views import slim_experiment_state


def _experiment_get_state_agent(
    *, experiments: Any, experiment_id: str, project_id: str | None = None
) -> dict[str, Any]:
    return slim_experiment_state(
        experiments.get_state(experiment_id=experiment_id, project_id=project_id)
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


def _mlflow_guidance(block: dict[str, Any]) -> str:
    if not block.get("configured"):
        return (
            "If you run a quantitative experiment, log it to MLflow — but no "
            "central MLflow tracking URI is configured on this backend yet."
        )
    return (
        "If you run a quantitative experiment, use MLflow: log params, metrics, "
        "and artifacts to the centralized server. Set the variables in mlflow.env "
        "(MLFLOW_TRACKING_URI, MLFLOW_EXPERIMENT_NAME, …) before the run, or use "
        "mlflow.autolog()."
    )


def _trace_run(run: dict[str, Any], *, with_history: bool) -> dict[str, Any]:
    """One run reduced to what the agent needs to reason about / plot: params,
    final metric values, and (optionally) the downsampled metric curves."""
    metrics = run.get("metrics") or {}
    out: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "run_name": run.get("run_name"),
        "status": run.get("status"),
        "params": run.get("params") or {},
        "metrics": {k: v.get("last") for k, v in metrics.items() if isinstance(v, dict)},
    }
    if with_history:
        out["history"] = run.get("history") or {}
    return out


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
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Map control/aggregate tool names to service methods.

    This is intentionally a thin registry: composition supplies the services,
    and ToolDispatcher verifies the final name set against TOOL_CONTRACTS.
    """
    def experiment_get_state_agent(
        *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        return _experiment_get_state_agent(
            experiments=experiments,
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
        result = experiments.transition(
            experiment_id=experiment_id,
            transition=transition,
            evidence=evidence,
            project_id=project_id,
        )
        # The moment an experiment starts running, hand the agent the MLflow
        # connection block so a quantitative run — including a local, non-sandbox
        # one — can log to the centralized server without hunting for the URI.
        if isinstance(result, dict) and result.get("status") == "running":
            block = _mlflow_connection(
                mlflow_tracking=mlflow_tracking,
                project_id=str(result.get("project_id") or project_id or ""),
                experiment_id=experiment_id,
            )
            result["mlflow"] = block
            result["mlflow_guidance"] = _mlflow_guidance(block)
        return result

    def experiment_mlflow_agent(
        *, experiment_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        state = experiments.get_state(experiment_id=experiment_id, project_id=project_id)
        block = _mlflow_connection(
            mlflow_tracking=mlflow_tracking,
            project_id=str(state.get("project_id") or project_id or ""),
            experiment_id=experiment_id,
        )
        return {
            "experiment_id": experiment_id,
            "mlflow": block,
            "guidance": _mlflow_guidance(block),
        }

    def mlflow_traces_agent(
        *,
        project_id: str | None = None,
        experiment_id: str | None = None,
        include_history: bool | None = None,
    ) -> dict[str, Any]:
        with_history = bool(experiment_id) if include_history is None else bool(include_history)
        if experiment_id:
            state = experiments.get_state(experiment_id=experiment_id, project_id=project_id)
            pid = state.get("project_id") or project_id
            targets = [(experiment_id, state.get("name") or experiment_id, state.get("status") or "")]
        else:
            pid = project_id
            listed = experiments.list_experiments(project_id=project_id)["experiments"]
            targets = [(e["id"], e.get("name") or e["id"], e.get("status") or "") for e in listed]
        out = []
        for eid, name, status in targets:
            data = (
                mlflow_tracking.results_metrics(project_id=str(pid or ""), experiment_id=eid)
                if mlflow_tracking is not None and pid
                else {
                    "experiment_id": eid,
                    "available": False,
                    "source": "mlflow",
                    "hint": "Centralized MLflow is not configured.",
                }
            )
            runs = [
                _trace_run(run, with_history=with_history)
                for captured in (data.get("experiments") or [])
                for run in (captured.get("runs") or [])
            ]
            out.append({
                "experiment_id": eid,
                "name": name,
                "status": status,
                "available": bool(data.get("available")),
                "runs": runs,
            })
        return {"experiments": out, "include_history": with_history}

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
        "experiment.mlflow": experiment_mlflow_agent,
        "mlflow.traces": mlflow_traces_agent,
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
        "review.status": reviews.status,
        "sandbox.options": sandboxes.options,
        "sandbox.get": sandboxes.get,
        "sandbox.list": sandboxes.list_sandboxes,
        "sandbox.release": sandboxes.release,
        "sandbox.terminal": sandboxes.terminal,
        "sandbox.health": sandboxes.health,
        "feed.register": feed.register,
        "feed.list": feed.list_posts,
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
    resource_register_file: Callable[..., dict[str, Any]],
    resource_associate: Callable[..., dict[str, Any]] | None = None,
    feed_post: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Map all local-mode tool names to service methods."""
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
    )
    handlers.update(
        {
            "resource.register_file": resource_register_file,
            "resource.associate": (
                resource_associate if resource_associate is not None else resources.associate
            ),
            "sandbox.request": sandboxes.request,
            "sandbox.attach": sandboxes.attach,
            "feed.post": feed_post if feed_post is not None else feed.post,
        }
    )
    return handlers
