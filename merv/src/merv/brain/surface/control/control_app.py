"""Unified brain app without checkout-local workspace/runtime wiring."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from merv.shared.storage_guidance import STORAGE_RULE_OF_THUMB, storage_guidance

from ...application.events import EventDispatcher
from ...application.experiments.create import CreateExperiment
from ...application.experiments.exhibits import ExperimentExhibits
from ...application.experiments.queries import ExperimentCollectionQuery
from ...application.experiments.reactions import ExperimentReactions
from ...application.experiments.tracking import (
    AgentExperimentQuery,
    ExperimentDetailQuery,
    FinalizeTrackingRun,
    GetTrackingContext,
)
from ...application.experiments.transition import TransitionExperiment
from ...application.queries import ComputeCostQuery, ExperimentFigureQuery, LogicGraphQuery, MlflowOverviewQuery, TenantCountersQuery
from ...application.resource_content import HostedResourceContentQuery
from ...application.reflections import ReflectionCommands
from ...application.status_guidance import StatusGuidancePolicy
from ...application.workflow import ProjectDashboardQuery, StatusAndNextQuery
from ...application.reviews import ReadReviewStatus
from ...application.tool_commands import ControlToolOperations
from ...artifacts.facade import ArtifactsFacade
from ...feed.facade import FeedFacade
from ...research_core.facade import ResearchCoreFacade
from ...research_core.snapshots import ResearchSnapshotReader
from ..tools.contracts import (
    CONTROL_PLANE_TOOL_NAMES,
    available_tool_names,
)
from .control_runtime import (
    ControlActivitySink,
    ControlSandboxWorker,
    ControlToolCallSink,
)
from ..observability import StructuredLogger
from ...kernel.ports.mgmt_keys import MgmtKeyStore
from ...kernel.ports.blob_store import EvidenceBlobStore
from .record_core import build_record_core
from ...sandbox.sandbox_backend import SandboxBackend
from ...sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES
from ...mlflow import CentralMlflowService
from ...sandbox.facade import SandboxFacade, SandboxReadFacade
from ...sandbox.runtime import build_sandbox_runtime
from ...object_storage.service import StorageLedgerService
from ...object_storage.catalog import StorageObjectCatalog
from ...kernel.state import BaseStateStore
from ..tools.tool_facade import ToolDispatcher
from ..tools.tool_handlers import build_control_tool_handlers


class ControlApp:
    """Brain app: record services, policy, and sandbox lifecycle; no checkout I/O."""

    def __init__(
        self,
        *,
        repo_root: Path,
        store: BaseStateStore,
        blobs: EvidenceBlobStore,
        storage: StorageLedgerService | None,
        execution_backend: SandboxBackend,
        task_channel: Any,
        mgmt_keys: MgmtKeyStore,
        mlflow_tracking: CentralMlflowService | None = None,
        force_expiry_reaper: bool = False,
    ) -> None:
        self.workspace = SimpleNamespace(repo_root=repo_root)
        self.store = store
        self.activity = ControlActivitySink()
        self.tool_calls = ControlToolCallSink()
        self.structured_logger = StructuredLogger()
        self.blobs = blobs
        self.storage = storage
        self.execution_backend = execution_backend
        self.mlflow_tracking = (
            mlflow_tracking
            if mlflow_tracking is not None
            else CentralMlflowService.from_env()
        )
        self.worker = ControlSandboxWorker()

        self.record_core = build_record_core(store=self.store, blobs=self.blobs)
        self.permissions = self.record_core.permissions
        self.quotas = self.record_core.quotas
        self.projects = self.record_core.projects
        self.claims = self.record_core.claims
        self.experiments = self.record_core.experiments
        self.resources = self.record_core.resources
        self.graph_refs = self.record_core.graph_refs
        self.reflection_waves = self.record_core.reflection_waves
        self.reviews = self.record_core.reviews
        self.feed = self.record_core.feed

        # Stable entrypoints serve cross-component use cases; aliases above
        # remain for deliberate single-owner delivery calls.
        self.research_core = ResearchCoreFacade(
            self.experiments,
            reflections=self.reflection_waves,
            graph_refs=self.graph_refs,
        )
        self.reflection_commands = ReflectionCommands(reflections=self.research_core)
        self.produced_objects = StorageObjectCatalog(store=self.store)
        self.artifacts = ArtifactsFacade(self.resources)
        self.hosted_resource_content_query = HostedResourceContentQuery(
            artifacts=self.artifacts
        )
        self.feed_api = FeedFacade(self.feed)
        self.experiment_exhibits = ExperimentExhibits(
            research=self.research_core,
            artifacts=self.artifacts,
            tracking=self.mlflow_tracking,
        )
        self.reaction_registry = EventDispatcher()
        self.experiment_reactions = ExperimentReactions(
            research=self.research_core,
            feed=self.feed_api,
            tracking=self.mlflow_tracking,
        )
        self.experiment_reactions.bind(self.reaction_registry)
        self.transition_experiment = TransitionExperiment(
            research=self.research_core,
            artifacts=self.artifacts,
            tracking=self.mlflow_tracking,
            exhibits=self.experiment_exhibits,
            dispatcher=self.reaction_registry,
            objects=self.produced_objects,
        )
        self.read_review_status = ReadReviewStatus(
            research=self.research_core,
            reviews=self.reviews,
            dispatcher=self.reaction_registry,
        )
        self.tracking_context = GetTrackingContext(
            research=self.research_core, tracking=self.mlflow_tracking
        )
        self.agent_experiment_query = AgentExperimentQuery(
            research=self.research_core,
            objects=self.produced_objects,
            tracking=self.mlflow_tracking,
        )
        self.experiment_detail_query = ExperimentDetailQuery(
            research=self.research_core,
            objects=self.produced_objects,
            tracking=self.mlflow_tracking,
        )
        self.finalize_tracking_run = FinalizeTrackingRun(
            research=self.research_core,
            feed=self.feed_api,
            tracking=self.mlflow_tracking,
            dispatcher=self.reaction_registry,
            objects=self.produced_objects,
        )
        self.experiment_collection_query = ExperimentCollectionQuery(
            research=self.research_core, objects=self.produced_objects
        )
        self.create_experiment = CreateExperiment(research=self.research_core)
        self.control_tool_operations = ControlToolOperations(
            project_create=self.projects.create,
            project_get=self.projects.get,
            claims_list=self.claims.list_claims,
            list_agent_experiments=self.experiment_collection_query.agent,
            resource_resolve=self.resources.resolve,
            resources_list=self.resources.list_resources,
            storage_resolve=self.storage.resolve if self.storage is not None else None,
            storage_list=self.storage.list_objects if self.storage is not None else None,
            storage_actions={name: getattr(self.storage, name) for name in ("pin", "unpin", "renew", "delete")} if self.storage is not None else {},
        )

        self.sandbox_runtime = build_sandbox_runtime(
            store=self.store,
            backend=self.execution_backend,
            mgmt_keys=mgmt_keys,
            tasks=task_channel,
            force_expiry_reaper=force_expiry_reaper,
        )
        self.sandboxes = SandboxFacade(
            worker=self.worker,
            runtime=self.sandbox_runtime,
            quotas=self.quotas,
            storage_enabled=self.storage is not None,
            # The sandbox module embeds/calls the component-owned values it is handed.
            storage_hint=STORAGE_RULE_OF_THUMB,
            attachment_check=self.research_core.assert_experiment_in_project,
        )
        self.sandbox_runtime.start()
        self.research_snapshots = ResearchSnapshotReader(
            store=self.store,
            experiments=self.experiments,
            reflections=self.reflection_waves,
        )
        self.sandbox_reads = SandboxReadFacade(
            store=self.store,
            reader=self.sandboxes,
        )
        self.next_action_policy = StatusGuidancePolicy(
            storage_enabled=self.storage is not None,
            storage_guidance=storage_guidance(enabled=self.storage is not None),
        )
        self.workflow = StatusAndNextQuery(
            snapshots=self.research_snapshots,
            sandboxes=self.sandbox_reads,
            policy=self.next_action_policy,
            objects=self.produced_objects,
        )
        self.project_dashboard_query = ProjectDashboardQuery(
            snapshots=self.research_snapshots,
            workflow=self.workflow,
            resources=self.resources.list_resources,
            review_queue=self.reviews.queue,
            recent_events=self.store.recent_events,
            health=lambda: self.mlflow_tracking.health(),
            current=self.projects.current,
        )
        self.home_query = self.project_dashboard_query
        self.project_overview = self.project_dashboard_query
        self.mlflow_overview_query = MlflowOverviewQuery(
            experiments=self.experiments.list_experiments,
            tracking=self.mlflow_tracking,
        )
        self.experiment_figure_query = ExperimentFigureQuery(
            experiment_state=self.research_core.experiment_state,
            review_snapshot=self.reviews.snapshot_from_id,
            open_reviews=self.reviews.open_requests_for_target,
            sandbox_row=self.sandboxes.get_row,
            sandbox_view=self.sandboxes.row_view,
            sandbox_status_active=ACTIVE_SANDBOX_STATUSES.__contains__,
        )
        self.compute_cost_query = ComputeCostQuery(
            project_spend=self.quotas.project_spend,
            experiments=self.research_core.project_experiments,
        )
        self.tenant_counters_query = TenantCountersQuery(
            event_count=self.store.tenant_event_count,
            generation_counters=self.quotas.tenant_generation_counters,
        )
        self.logic_graph_query = LogicGraphQuery(
            research=self.research_core,
            artifacts=self.artifacts,
        )
        control_tool_names = set(CONTROL_PLANE_TOOL_NAMES)
        control_tool_names &= available_tool_names(storage_enabled=self.storage is not None)
        self.tools = ToolDispatcher(
            handlers=build_control_tool_handlers(
                workflow=self.workflow,
                projects=self.projects,
                claims=self.claims,
                experiments=SimpleNamespace(create=self.create_experiment),
                reflection_tools=self.reflection_commands,
                resources=self.resources,
                storage=self.storage,
                reviews=self.reviews,
                sandboxes=self.sandboxes,
                feed=self.feed,
                experiment_transition=self.transition_experiment,
                experiment_exhibit=self.experiment_exhibits,
                tracking_context=SimpleNamespace(
                    execute=self.tracking_context.execute,
                    experiment=self.agent_experiment_query,
                ),
                tracking_finalize=self.finalize_tracking_run,
                review_status=self.read_review_status,
                operations=self.control_tool_operations,
            ),
            permissions=self.permissions,
            activity=self.activity,
            tool_calls=self.tool_calls,
            tool_names=control_tool_names,
        )

    def current_project(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.project_overview.current_project(tenant_id=tenant_id)

    def list_tools(self) -> list[dict[str, Any]]:
        return self.tools.list_tools()

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        activity_source: str = "app",
        internal_kwargs: dict[str, Any] | None = None,
        telemetry_project_id: str | None = None,
    ) -> dict[str, Any]:
        return self.tools.call_tool(
            name=name,
            arguments=arguments,
            activity_source=activity_source,
            internal_kwargs=internal_kwargs,
            telemetry_project_id=telemetry_project_id,
        )

    def shutdown(self) -> None:
        with suppress(Exception):
            self.sandbox_runtime.shutdown()
        with suppress(Exception):
            self.execution_backend.shutdown()
