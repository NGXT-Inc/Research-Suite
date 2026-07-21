"""Tool-name registry over composed service objects."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from ...application.facade import ReflectionCommands
from .contracts import TOOL_MANIFEST, available_tool_names


def build_control_tool_handlers(
    *,
    workflow: Any,
    projects: Any,
    claims: Any,
    experiments: Any,
    reflection_tools: ReflectionCommands,
    resources: Any,
    storage: Any | None,
    reviews: Any,
    sandboxes: Any,
    feed: Any,
    experiment_transition: Any,
    experiment_exhibit: Any,
    tracking_context: Any,
    tracking_finalize: Any,
    review_status: Any,
    operations: Any,
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Map control-plane tool names to service methods.

    This is intentionally a thin registry: composition supplies the services,
    and ToolDispatcher verifies the final name set against TOOL_CONTRACTS.
    """
    def experiment_transition_agent(
        *,
        experiment_id: str,
        transition: str,
        evidence: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return experiment_transition.execute(
            experiment_id=experiment_id,
            transition=transition,
            evidence=evidence,
            project_id=project_id,
            include_tracking_credentials=True,
        )

    owners = {
        "workflow": workflow,
        "operations": operations,
        "projects": projects,
        "claims": claims,
        "experiments": experiments,
        "experiment_transition": SimpleNamespace(agent=experiment_transition_agent),
        "experiment_exhibit": experiment_exhibit,
        "tracking_context": tracking_context,
        "tracking_finalize": tracking_finalize,
        "reflection_tools": reflection_tools,
        "resources": resources,
        "reviews": reviews,
        "review_status": review_status,
        "sandboxes": sandboxes,
        "feed": feed,
    }
    if storage is not None:
        owners["storage"] = storage
    available = available_tool_names(storage_enabled=storage is not None)
    handlers: dict[str, Callable[..., dict[str, Any]]] = {}
    for name, tool in TOOL_MANIFEST.items():
        if name not in available or tool.plane != "control":
            continue
        owner_name, method_name = tool.handler_identity.split(".", 1)
        handlers[name] = getattr(owners[owner_name], method_name)
    return handlers
