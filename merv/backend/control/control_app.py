"""Unified brain app without checkout-local workspace/runtime wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..tools.contracts import (
    CONTROL_PLANE_TOOL_NAMES,
    available_tool_names,
)
from .control_runtime import (
    ControlActivitySink,
    ControlSandboxWorker,
    ControlToolCallSink,
)
from ..domain.storage_guidance import STORAGE_RULE_OF_THUMB, storage_guidance
from ..observability import StructuredLogger
from ..ports.mgmt_keys import MgmtKeyStore
from .record_core import build_experiment_attachment_check, build_record_core
from ..sandbox.sandbox_backend import SandboxBackend
from ..mlflow import CentralMlflowService
from ..services.sandbox.sandboxes import SandboxService
from ..storage.service import StorageLedgerService
from ..services.workflow import WorkflowService
from ..state import BaseStateStore
from ..storage.blobs import BlobStore
from ..tools.tool_facade import ToolDispatcher
from ..tools.tool_handlers import build_control_tool_handlers


class ControlApp:
    """Brain app: record services, policy, and sandbox lifecycle; no checkout I/O."""

    def __init__(
        self,
        *,
        repo_root: Path,
        store: BaseStateStore,
        blobs: BlobStore,
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
        self.reflection_tools = self.record_core.reflection_tools
        self.reflections = self.reflection_tools
        self.project_overview = self.record_core.project_overview
        self.reviews = self.record_core.reviews
        self.feed = self.record_core.feed

        self.sandboxes = SandboxService(
            store=self.store,
            sandbox_backend=self.execution_backend,
            worker=self.worker,
            activity=None,
            mgmt_keys=mgmt_keys,
            quotas=self.quotas,
            task_channel=task_channel,
            storage_enabled=self.storage is not None,
            # Guidance prose + the experiment-label check are surface-owned;
            # the sandbox module embeds/calls what it is handed.
            storage_hint=STORAGE_RULE_OF_THUMB,
            attachment_check=build_experiment_attachment_check(store=self.store),
            # Hosted control pays the provider bill; the composition root
            # (composition/control_mode.py) passes True so the env off-switch
            # cannot leave billing VMs unreaped.
            force_expiry_reaper=force_expiry_reaper,
        )
        self.workflow = WorkflowService(
            store=self.store,
            experiments=self.experiments,
            reviews=self.reviews,
            sandboxes=self.sandboxes,
            reflections=self.reflection_waves,
            storage_enabled=self.storage is not None,
            storage_guidance=storage_guidance(enabled=self.storage is not None),
        )
        control_tool_names = set(CONTROL_PLANE_TOOL_NAMES)
        control_tool_names &= available_tool_names(storage_enabled=self.storage is not None)
        self.tools = ToolDispatcher(
            handlers=build_control_tool_handlers(
                workflow=self.workflow,
                projects=self.projects,
                project_overview=self.project_overview,
                claims=self.claims,
                experiments=self.experiments,
                reflections=self.reflections,
                resources=self.resources,
                storage=self.storage,
                reviews=self.reviews,
                sandboxes=self.sandboxes,
                mlflow_tracking=self.mlflow_tracking,
                feed=self.feed,
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
        try:
            self.sandboxes.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.execution_backend.shutdown()
        except Exception:  # noqa: BLE001
            pass
