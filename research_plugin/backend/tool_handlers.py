"""Tool-name registry over composed service objects."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def build_control_tool_handlers(
    *,
    workflow: Any,
    projects: Any,
    project_overview: Any,
    claims: Any,
    experiments: Any,
    reflections: Any,
    resources: Any,
    reviews: Any,
    sandboxes: Any,
    feed: Any,
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Map control/aggregate tool names to service methods.

    This is intentionally a thin registry: composition supplies the services,
    and ToolDispatcher verifies the final name set against TOOL_CONTRACTS.
    """
    return {
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
        "experiment.list": experiments.list_experiments_agent,
        "experiment.get_state": experiments.get_state_agent,
        "experiment.transition": experiments.transition,
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


def build_local_tool_handlers(
    *,
    workflow: Any,
    projects: Any,
    project_overview: Any,
    claims: Any,
    experiments: Any,
    reflections: Any,
    resources: Any,
    reviews: Any,
    sandboxes: Any,
    feed: Any,
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
        reviews=reviews,
        sandboxes=sandboxes,
        feed=feed,
    )
    handlers.update(
        {
            "resource.register_file": resources.register_file,
            "resource.associate": resources.associate,
            "sandbox.request": sandboxes.request,
            "sandbox.sync": sandboxes.sync,
            "feed.post": feed_post if feed_post is not None else feed.post,
        }
    )
    return handlers
