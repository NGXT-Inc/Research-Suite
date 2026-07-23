"""Unified brain app without checkout-local workspace/runtime wiring."""

from __future__ import annotations

from contextlib import suppress
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
from ...application.timeline import EventTimelineQuery
from ...application.reflections import ReflectionCommands
from ...application.status_guidance import StatusGuidancePolicy
from ...application.workflow import ProjectDashboardQuery, StatusAndNextQuery
from ...application.reviews import ReadReviewStatus
from ...application.tool_commands import ControlToolOperations
from ...artifacts.facade import ArtifactsFacade
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
from ..user_settings import UserHfTokenSettings
from ...kernel.ports.mgmt_keys import MgmtKeyStore
from ...kernel.ports.blob_store import EvidenceBlobStore
from .record_core import build_record_core
from ...sandbox.sandbox_backend import SandboxBackend
from ...sandbox.sandbox_support import ACTIVE_SANDBOX_STATUSES
from ...mlflow import CentralMlflowService
from ...sandbox.facade import SandboxFacade
from ...sandbox.runtime import build_sandbox_runtime
from ...object_storage.service import StorageLedgerService
from ...object_storage.catalog import StorageObjectCatalog
from ...kernel.state import BaseStateStore
from ..tools.tool_facade import ToolDispatcher
from ..tools.tool_handlers import build_control_tool_handlers
from ..transport.api.dependencies import HttpDependencies


class ControlApp:
    """Brain app: record services, policy, and sandbox lifecycle; no checkout I/O."""

    def __init__(
        self,
        *,
        store: BaseStateStore,
        blobs: EvidenceBlobStore,
        storage: StorageLedgerService | None,
        execution_backend: SandboxBackend,
        task_channel: Any,
        mgmt_keys: MgmtKeyStore,
        mlflow_tracking: CentralMlflowService | None = None,
        force_expiry_reaper: bool = False,
        structured_logging: bool = False,
    ) -> None:
        self._store = store
        self.activity = ControlActivitySink()
        self.tool_calls = ControlToolCallSink()
        self.structured_logger = StructuredLogger(enabled=structured_logging)
        self._blobs = blobs
        self._storage = storage
        self._execution_backend = execution_backend
        self._tracking = (
            mlflow_tracking
            if mlflow_tracking is not None
            else CentralMlflowService.from_env()
        )
        self._worker = ControlSandboxWorker()
        core = self._record_core = build_record_core(store=store, blobs=blobs)

        self.research_core = ResearchCoreFacade(
            core.experiments,
            reflections=core.reflection_waves,
            graph_refs=core.graph_refs,
        )
        self.reflection_commands = ReflectionCommands(reflections=self.research_core)
        self.produced_objects = StorageObjectCatalog(store=store)
        self.artifacts = ArtifactsFacade(submissions=core.artifact_submissions)
        self.experiment_exhibits = ExperimentExhibits(
            research=self.research_core,
            artifacts=self.artifacts,
            tracking=self._tracking,
        )
        self.reaction_registry = EventDispatcher()
        self.experiment_reactions = ExperimentReactions(
            research=self.research_core,
            feed=core.feed,
            tracking=self._tracking,
        )
        self.experiment_reactions.bind(self.reaction_registry)
        self.transition_experiment = TransitionExperiment(
            research=self.research_core,
            artifacts=self.artifacts,
            tracking=self._tracking,
            exhibits=self.experiment_exhibits,
            dispatcher=self.reaction_registry,
            objects=self.produced_objects,
        )
        self.read_review_status = ReadReviewStatus(
            research=self.research_core,
            reviews=core.reviews,
            dispatcher=self.reaction_registry,
        )
        self.tracking_context = GetTrackingContext(
            research=self.research_core, tracking=self._tracking
        )
        self.agent_experiment_query = AgentExperimentQuery(
            research=self.research_core,
            objects=self.produced_objects,
            tracking=self._tracking,
        )
        self.experiment_detail_query = ExperimentDetailQuery(
            research=self.research_core,
            objects=self.produced_objects,
            tracking=self._tracking,
        )
        self.finalize_tracking_run = FinalizeTrackingRun(
            research=self.research_core,
            feed=core.feed,
            tracking=self._tracking,
            dispatcher=self.reaction_registry,
            objects=self.produced_objects,
        )
        self.experiment_collection_query = ExperimentCollectionQuery(
            research=self.research_core, objects=self.produced_objects
        )
        self.create_experiment = CreateExperiment(research=self.research_core)
        self.control_tool_operations = ControlToolOperations(
            projects=core.projects,
            claims=core.claims,
            experiments=self.experiment_collection_query,
            storage=storage,
        )

        self._sandbox_runtime = build_sandbox_runtime(
            store=store,
            backend=execution_backend,
            mgmt_keys=mgmt_keys,
            tasks=task_channel,
            force_expiry_reaper=force_expiry_reaper,
        )
        self.sandboxes = SandboxFacade(
            worker=self._worker,
            runtime=self._sandbox_runtime,
            quotas=core.quotas,
            storage_enabled=storage is not None,
            # The sandbox module embeds/calls the component-owned values it is handed.
            storage_hint=STORAGE_RULE_OF_THUMB,
            attachment_check=self.research_core.assert_experiment_in_project,
        )
        self._sandbox_runtime.start()
        self.research_snapshots = ResearchSnapshotReader(
            store=store,
            experiments=core.experiments,
            reflections=core.reflection_waves,
        )
        self.next_action_policy = StatusGuidancePolicy(
            storage_enabled=storage is not None,
            storage_guidance=storage_guidance(enabled=storage is not None),
        )
        self.workflow = StatusAndNextQuery(
            snapshots=self.research_snapshots,
            sandboxes=self.sandboxes,
            policy=self.next_action_policy,
            objects=self.produced_objects,
        )
        self.project_dashboard_query = ProjectDashboardQuery(
            snapshots=self.research_snapshots,
            workflow=self.workflow,
            artifacts=core.artifact_submissions.find,
            review_queue=core.reviews.queue,
            recent_events=store.recent_events,
            health=lambda: self._tracking.health(),
            current=core.projects.current,
        )
        self.mlflow_overview_query = MlflowOverviewQuery(
            experiments=self.research_core.project_experiment_summaries,
            tracking=self._tracking,
        )
        self.experiment_figure_query = ExperimentFigureQuery(
            experiment_state=self.research_core.experiment_state,
            review_snapshot=core.reviews.snapshot_from_id,
            open_reviews=core.reviews.open_requests_for_target,
            sandbox_row=self.sandboxes.get_row,
            sandbox_view=self.sandboxes.row_view,
            sandbox_status_active=ACTIVE_SANDBOX_STATUSES.__contains__,
        )
        self.compute_cost_query = ComputeCostQuery(
            project_spend=core.quotas.project_spend,
            experiments=self.research_core.project_experiment_summaries,
        )
        self.tenant_counters_query = TenantCountersQuery(
            event_count=store.tenant_event_count,
            generation_counters=core.quotas.tenant_generation_counters,
        )
        self.logic_graph_query = LogicGraphQuery(
            research=self.research_core,
            artifacts=self.artifacts,
        )
        self.event_timeline = EventTimelineQuery(
            source=store,
        )
        self.user_settings = UserHfTokenSettings(store=store)
        control_tool_names = set(CONTROL_PLANE_TOOL_NAMES)
        control_tool_names &= available_tool_names(storage_enabled=storage is not None)
        self.tools = ToolDispatcher(
            handlers=build_control_tool_handlers(
                workflow=self.workflow,
                projects=core.projects,
                claims=core.claims,
                create_experiment=self.create_experiment,
                reflection_tools=self.reflection_commands,
                artifact_submissions=core.artifact_submissions,
                storage=storage,
                reviews=core.reviews,
                sandboxes=self.sandboxes,
                feed=core.feed,
                experiment_transition=self.transition_experiment,
                experiment_exhibit=self.experiment_exhibits,
                tracking_context=self.tracking_context,
                agent_experiment=self.agent_experiment_query,
                tracking_finalize=self.finalize_tracking_run,
                review_status=self.read_review_status,
                operations=self.control_tool_operations,
                litreview=core.literature,
            ),
            permissions=core.permissions,
            activity=self.activity,
            tool_calls=self.tool_calls,
            tool_names=control_tool_names,
        )
        self.http = HttpDependencies(
            projects=core.projects,
            reviews=core.reviews,
            artifact_submissions=core.artifact_submissions,
            feed=core.feed,
            sandboxes=self.sandboxes,
            storage=storage,
            timeline=self.event_timeline,
            activity=self.activity,
            tool_calls=self.tool_calls,
            tools=self.tools,
            structured_log=self.structured_logger,
            experiment_detail=self.experiment_detail_query,
            experiment_collection=self.experiment_collection_query,
            compute_cost=self.compute_cost_query,
            logic_graph=self.logic_graph_query,
            workflow=self.workflow,
            dashboard=self.project_dashboard_query,
            experiment_figure=self.experiment_figure_query,
            tracking_overview=self.mlflow_overview_query,
            tenant_counters=self.tenant_counters_query,
            literature=core.literature,
            user_settings=self.user_settings,
        )

    def shutdown(self) -> None:
        with suppress(Exception):
            self._sandbox_runtime.shutdown()
        with suppress(Exception):
            self._execution_backend.shutdown()
