"""Tool-name registry over composed service objects."""

from __future__ import annotations

from ...application.facade import (
    AgentExperimentQuery,
    ControlToolOperations,
    CreateExperiment,
    ExperimentExhibits,
    FinalizeTrackingRun,
    GetTrackingContext,
    ReadReviewStatus,
    ReflectionCommands,
    StatusAndNextQuery,
    TransitionExperiment,
)
from ...application.ports.storage import ObjectStorage
from ...artifacts.facade import ArtifactRecords
from ...feed.facade import FeedDelivery
from ...research_core.facade import (
    ResearchClaims,
    ResearchLiterature,
    ResearchProjects,
    ResearchReviewDelivery,
)
from ...sandbox.facade import SandboxFacade
from .contracts import TOOL_MANIFEST, available_tool_names
from .tool_facade import ToolHandler


def build_control_tool_handlers(
    *,
    workflow: StatusAndNextQuery,
    projects: ResearchProjects,
    claims: ResearchClaims,
    create_experiment: CreateExperiment,
    reflection_tools: ReflectionCommands,
    resources: ArtifactRecords,
    storage: ObjectStorage | None,
    reviews: ResearchReviewDelivery,
    sandboxes: SandboxFacade,
    feed: FeedDelivery,
    experiment_transition: TransitionExperiment,
    experiment_exhibit: ExperimentExhibits,
    tracking_context: GetTrackingContext,
    agent_experiment: AgentExperimentQuery,
    tracking_finalize: FinalizeTrackingRun,
    review_status: ReadReviewStatus,
    operations: ControlToolOperations,
    litreview: ResearchLiterature,
) -> dict[str, ToolHandler]:
    """Map control-plane tool names to service methods.

    This is intentionally a thin registry: composition supplies the services,
    and ToolDispatcher verifies the final name set against TOOL_CONTRACTS.
    """
    owners = {
        "workflow": workflow,
        "operations": operations,
        "projects": projects,
        "claims": claims,
        "create_experiment": create_experiment,
        "agent_experiment": agent_experiment,
        "experiment_transition": experiment_transition,
        "experiment_exhibit": experiment_exhibit,
        "tracking_context": tracking_context,
        "tracking_finalize": tracking_finalize,
        "reflection_tools": reflection_tools,
        "resources": resources,
        "reviews": reviews,
        "review_status": review_status,
        "sandboxes": sandboxes,
        "feed": feed,
        "litreview": litreview,
    }
    if storage is not None:
        owners["storage"] = storage
    available = available_tool_names(storage_enabled=storage is not None)
    handlers: dict[str, ToolHandler] = {}
    for name, tool in TOOL_MANIFEST.items():
        if name not in available or tool.plane != "control":
            continue
        owner_name, method_name = tool.handler_identity.split(".", 1)
        handlers[name] = getattr(owners[owner_name], method_name)
    return handlers
