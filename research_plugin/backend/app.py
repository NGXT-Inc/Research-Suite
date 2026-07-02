"""Application composition root and MCP tool facade."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import build_object_store
from .services.sandbox.sandboxes import SandboxService
from .storage.service import StorageLedgerService
from .services.workflow import WorkflowService
from .state import BaseStateStore, StateStore
from .state.blobs import BlobStore
from .observability import StructuredLogger
from .control.record_core import build_record_core
from .mlflow import CentralMlflowService
from .tools.tool_facade import ToolDispatcher
from .tools.contracts import available_tool_names
from .tools.tool_handlers import build_local_tool_handlers
from .utils import ValidationError
from .dataplane.resource_validation import validate_local_resource_artifact
from .dataplane.results_tsv import merge_results_tsv

if TYPE_CHECKING:
    from .local_runtime import LocalRuntime
    from .sandbox.sandbox_backend import SandboxBackend


class ResearchPluginApp:
    """Composes isolated components behind tool-call contracts."""

    def __init__(
        self,
        *,
        repo_root: Path,
        db_path: Path,
        execution_backend: SandboxBackend | None = None,
        store: BaseStateStore | None = None,
        blobs: "BlobStore | None" = None,
        storage: StorageLedgerService | None = None,
        task_channel: Any | None = None,
        runtime: "LocalRuntime | None" = None,
        mlflow_tracking: CentralMlflowService | None = None,
    ) -> None:
        # The plane seam (cloud plan Phase 3): the record store knows nothing
        # about the checkout; local paths flow from the workspace and every
        # local-IO duty routes through the data-plane worker. This constructor
        # IS the local-mode composition — it binds both planes in one process.
        if runtime is None:
            from .local_runtime import build_local_runtime

            runtime = build_local_runtime(
                repo_root=repo_root,
                execution_backend=execution_backend,
                blobs=blobs,
            )
        elif any(value is not None for value in (execution_backend, blobs)):
            raise ValueError(
                "runtime cannot be combined with execution_backend, "
                "or blobs"
            )
        self.workspace = runtime.workspace
        # Store injection (cloud plan Phase 6): the dual-dialect contract
        # tests hand in a PostgresStateStore; absent that, local mode builds
        # its SQLite store at db_path exactly as before. The control profile
        # (Phase 8) injects a PostgresStateStore + S3BlobStore via the control
        # composition root rather than db_url plumbing through here.
        self.store = store if store is not None else StateStore(db_path=db_path)
        # Telemetry sinks are machine-local by construction: composition hands
        # them explicit paths (the control composition gets its own sinks).
        self.activity = runtime.activity
        # Full-fidelity tool-call recorder backing the debug analyzer. Isolated in
        # its own SQLite file so its churn never touches the state DB.
        self.tool_calls = runtime.tool_calls
        # Structured cloud log stream (cloud plan Phase 9): one redacted JSON
        # line per tool call / HTTP request to stdout, in control mode only.
        # Dormant (disabled) in local mode, so behavior is byte-identical.
        self.structured_logger = StructuredLogger()
        # Content-addressed store for gated-artifact bytes and figures. Local
        # mode roots it next to the state DB; the control composition injects an
        # S3BlobStore. Same protocol, same contract tests, so the rest of the
        # app is blob-impl-blind.
        self.blobs = runtime.blobs
        if storage is not None:
            self.storage = storage
        else:
            objects = build_object_store(default_root=self.workspace.research_dir)
            self.storage = (
                StorageLedgerService(store=self.store, objects=objects)
                if objects is not None
                else None
            )
        self.execution_backend = runtime.execution_backend
        self.mlflow_tracking = (
            mlflow_tracking
            if mlflow_tracking is not None
            else CentralMlflowService.from_env()
        )
        self.worker = runtime.worker
        self.feed_image_reader = runtime.feed_image_reader
        self.resource_artifact_reader = runtime.resource_artifact_reader
        self.resource_observer = runtime.resource_observer
        self.record_core = build_record_core(store=self.store, blobs=self.blobs)
        self.permissions = self.record_core.permissions
        self.quotas = self.record_core.quotas
        self.projects = self.record_core.projects
        self.claims = self.record_core.claims
        self.experiments = self.record_core.experiments
        self.resources = self.record_core.resources
        self.graph_refs = self.record_core.graph_refs
        self.syntheses = self.record_core.syntheses
        self.reflections = self.record_core.reflections
        self.project_overview = self.record_core.project_overview
        self.reviews = self.record_core.reviews
        self.feed = self.record_core.feed
        # One-time local upgrade: capture bytes for gated associations made
        # before byte capture existed (idempotent, skips present blobs).
        self._backfill_gated_blobs()
        self.sandboxes = SandboxService(
            store=self.store,
            sandbox_backend=self.execution_backend,
            worker=self.worker,
            activity=self.activity,
            # Per-sandbox management keys (plan Phase 5): control-plane
            # custody — local mode roots them under .research_plugin/ beside
            # the rest of the control state.
            mgmt_keys=runtime.mgmt_keys,
            quotas=self.quotas,
            # Split mode (Phase 8): the control composition injects an
            # HttpTaskChannel so control enqueues data-plane work to the daemon
            # over HTTP. None ⇒ the synchronous in-process channel (local mode).
            task_channel=(
                task_channel if task_channel is not None else runtime.task_channel
            ),
            storage_enabled=self.storage is not None,
        )
        self.workflow = WorkflowService(
            store=self.store,
            experiments=self.experiments,
            reviews=self.reviews,
            sandboxes=self.sandboxes,
            syntheses=self.syntheses,
            storage_enabled=self.storage is not None,
        )
        self.tools = ToolDispatcher(
            handlers=build_local_tool_handlers(
                workflow=self.workflow,
                projects=self.projects,
                project_overview=self.project_overview,
                claims=self.claims,
                experiments=self.experiments,
                reflections=self.reflections,
                resources=self.resources,
                storage=self.storage,
                resource_register_file=self.register_resource_file,
                resource_validate=self.validate_resource_file,
                resource_associate=self.associate_resource,
                experiment_materialize_folders=self.materialize_experiment_folders,
                results_merge_tsv=self.merge_results_tsv,
                reviews=self.reviews,
                sandboxes=self.sandboxes,
                mlflow_tracking=self.mlflow_tracking,
                feed=self.feed,
                feed_post=self.post_feed,
                storage_upload_file=self.upload_storage_file,
                storage_download_file=self.download_storage_file,
            ),
            permissions=self.permissions,
            activity=self.activity,
            tool_calls=self.tool_calls,
            tool_names=available_tool_names(storage_enabled=self.storage is not None),
        )

    def current_project(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.project_overview.current_project(tenant_id=tenant_id)

    def list_tools(self) -> list[dict[str, Any]]:
        return self.tools.list_tools()

    def _backfill_gated_blobs(self) -> int:
        backfilled = 0
        for candidate in self.resources.gated_blob_backfill_candidates():
            artifact = self.resource_artifact_reader.read_for_backfill(
                path=str(candidate.get("path") or ""),
                role=str(candidate.get("role") or ""),
            )
            if artifact is None:
                continue
            if self.resources.record_backfilled_gated_blob(
                version_id=str(candidate.get("version_id") or ""),
                project_id=str(candidate.get("project_id") or ""),
                role=str(candidate.get("role") or ""),
                content_bytes=artifact["content_bytes"],
                figures=artifact.get("figures") or [],
            ):
                backfilled += 1
        return backfilled

    def register_resource_file(
        self,
        *,
        path: str | None = None,
        paths: list[str] | None = None,
        kind: str = "other",
        title: str = "",
        created_by: str = "codex",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Observe local file(s), then submit record-safe observations."""
        if paths:
            resources = [
                self._record_observed_resource(
                    path=p,
                    kind=kind,
                    title=title,
                    created_by=created_by,
                    project_id=project_id,
                )
                for p in paths
            ]
            return {"resources": resources, "count": len(resources)}
        if not path:
            raise ValidationError(
                "resource.register_file requires 'path' (a single file) or 'paths' (a batch)"
            )
        return self._record_observed_resource(
            path=path,
            kind=kind,
            title=title,
            created_by=created_by,
            project_id=project_id,
        )

    def _record_observed_resource(
        self,
        *,
        path: str,
        kind: str = "other",
        title: str = "",
        created_by: str = "codex",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        observation = self.resource_observer.observe_file(
            path=path,
            kind=kind,
            title=title,
            created_by=created_by,
        )
        return self.resources.record_observation(
            project_id=project_id,
            **observation,
        )

    def validate_resource_file(
        self,
        *,
        path: str,
        role: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            self.store.require_project_id(conn=conn, project_id=project_id)
        finally:
            conn.close()
        return validate_local_resource_artifact(
            repo_root=self.workspace.repo_root,
            path=path,
            role=role,
        )

    def associate_resource(
        self,
        *,
        resource_id: str,
        target_type: str,
        target_id: str,
        role: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        validation = self.resources.validate_association_intent(
            resource_id=resource_id,
            target_type=target_type,
            target_id=target_id,
            role=role,
            project_id=project_id,
        )
        resource = validation.get("resource") or {}
        actual_project_id = str(resource.get("project_id") or project_id or "")
        path = str(resource.get("path") or "")
        if not path:
            raise ValidationError(f"resource has no path: {resource_id}")
        observation = self.resource_observer.observe_file(
            path=path,
            kind=str(resource.get("kind") or "other"),
            title=str(resource.get("title") or ""),
            created_by=str(resource.get("created_by") or "codex"),
        )
        self.resources.record_observation(
            project_id=actual_project_id,
            **observation,
        )
        artifact = self.resource_artifact_reader.read_for_association(
            path=path,
            role=role,
        )
        return self.resources.associate_observed(
            resource_id=resource_id,
            target_type=target_type,
            target_id=target_id,
            role=role,
            project_id=actual_project_id,
            content_bytes=artifact.get("content_bytes"),
            figures=artifact.get("figures") or [],
        )

    def merge_results_tsv(
        self,
        *,
        source_path: str,
        target_path: str,
        key_columns: list[str] | None = None,
        dry_run: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            self.store.require_project_id(conn=conn, project_id=project_id)
        finally:
            conn.close()
        return merge_results_tsv(
            repo_root=self.workspace.repo_root,
            source_path=source_path,
            target_path=target_path,
            key_columns=key_columns,
            dry_run=dry_run,
        )

    def materialize_experiment_folders(
        self,
        *,
        experiment_id: str | None = None,
        status: str | None = "planned",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        from .dataplane.experiment_folders import materialize_experiment_folders

        if experiment_id:
            experiments = [
                self.experiments.get_state(
                    experiment_id=experiment_id,
                    project_id=project_id,
                )
            ]
        else:
            experiments = self.experiments.list_experiments(project_id=project_id)[
                "experiments"
            ]
            if status:
                experiments = [
                    experiment
                    for experiment in experiments
                    if experiment.get("status") == status
                ]
        return materialize_experiment_folders(
            repo_root=self.workspace.repo_root,
            experiments=experiments,
        )

    def upload_storage_file(
        self,
        *,
        path: str,
        kind: str,
        name: str = "",
        content_type: str = "",
        created_by: str = "codex",
        producing_experiment_id: str = "",
        producing_run: str = "",
        source_uri: str = "",
        notes: str = "",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if self.storage is None:
            raise ValidationError("storage is not configured")
        local_path = self._resolve_project_path(path)
        object_name = name or self._default_storage_name(local_path)
        return self.storage.upload_file(
            project_id=project_id,
            path=local_path,
            name=object_name,
            kind=kind,
            content_type=content_type,
            created_by=created_by,
            producing_experiment_id=producing_experiment_id,
            producing_run=producing_run,
            source_uri=source_uri,
            notes=notes,
        )

    def download_storage_file(
        self,
        *,
        path: str,
        object_id: str | None = None,
        name: str | None = None,
        version: int | None = None,
        overwrite: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if self.storage is None:
            raise ValidationError("storage is not configured")
        local_path = self._resolve_project_path(path)
        return self.storage.download_file(
            project_id=project_id,
            path=local_path,
            object_id=object_id,
            name=name,
            version=version,
            overwrite=overwrite,
        )

    def _resolve_project_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace.repo_root / candidate
        return candidate

    def _default_storage_name(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(
                self.workspace.repo_root.resolve()
            ).as_posix()
        except ValueError:
            return path.name

    def post_feed(
        self,
        *,
        handle: str,
        text: str,
        image_path: str | None = None,
        url: str | None = None,
        ref: str | None = None,
        kind: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if image_path:
            self.feed.validate_post_intent(
                handle=handle,
                text=text,
                ref=ref,
                kind=kind,
                project_id=project_id,
            )
        image = (
            self.feed_image_reader.read_image(path=image_path)
            if image_path
            else None
        )
        return self.feed.post_observed(
            handle=handle,
            text=text,
            image_path=str(image["path"]) if image else None,
            image_bytes=image["data"] if image else None,
            url=url,
            ref=ref,
            kind=kind,
            project_id=project_id,
        )

    def shutdown(self) -> None:
        """Best-effort: stop background provisioning jobs and backend resources."""
        try:
            self.sandboxes.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.execution_backend.shutdown()
        except Exception:  # noqa: BLE001
            pass
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
