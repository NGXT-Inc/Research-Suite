"""Tool-name registry over composed service objects."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..mlflow import mlflow_experiment_name
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
        resolved_project_id = str(result.get("project_id") or project_id or "")
        slim = slim_experiment_state(result)
        # The moment an experiment starts running, hand the agent the MLflow
        # connection block so a quantitative run — including a local, non-sandbox
        # one — can log to the centralized server without hunting for the URI.
        if slim.get("status") == "running":
            block = _mlflow_connection(
                mlflow_tracking=mlflow_tracking,
                project_id=resolved_project_id,
                experiment_id=experiment_id,
            )
            slim["mlflow"] = block
            slim["mlflow_guidance"] = _mlflow_guidance(block)
        return slim

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
        return _mlflow_context_response(
            project_id=resolved_project_id,
            experiment_id=experiment_id,
            mlflow=block,
        )

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
        "mlflow.context": mlflow_context_agent,
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
    )
    handlers.update(
        {
            "resource.register_file": resource_register_file,
            "resource.associate": (
                resource_associate if resource_associate is not None else resources.associate
            ),
            "resource.associate_batch": resource_associate_batch,
            "sandbox.request": sandboxes.request,
            "sandbox.attach": sandboxes.attach,
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
