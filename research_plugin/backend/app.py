"""Application composition root and MCP tool facade."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .services.claims import ClaimService
from .services.experiments import ExperimentService
from .services.feed import FeedService
from .services.permissions import PermissionService
from .services.project_overview import ProjectOverviewService
from .services.quotas import QuotaService
from .services.reflection_tools import ReflectionToolService
from .services.projects import ProjectService
from .services.resources import ResourceService
from .services.reviews import ReviewService
from .services.sandboxes import SandboxService
from .services.syntheses import SynthesisService
from .services.workflow import WorkflowService
from .state import BaseStateStore, StateStore
from .state.blobs import BlobStore
from .observability import StructuredLogger
from .tool_facade import ToolDispatcher
from .tool_handlers import build_local_tool_handlers
from .utils import ValidationError

if TYPE_CHECKING:
    from .execution.ssh_rsync import SshRsyncSyncer
    from .local_runtime import LocalRuntime
    from .sandbox_backend import SandboxBackend


class ResearchPluginApp:
    """Composes isolated components behind tool-call contracts."""

    def __init__(
        self,
        *,
        repo_root: Path,
        db_path: Path,
        execution_backend: SandboxBackend | None = None,
        rsync_syncer: SshRsyncSyncer | None = None,
        store: BaseStateStore | None = None,
        blobs: "BlobStore | None" = None,
        task_channel: Any | None = None,
        runtime: "LocalRuntime | None" = None,
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
                rsync_syncer=rsync_syncer,
                blobs=blobs,
            )
        elif any(
            value is not None for value in (execution_backend, rsync_syncer, blobs)
        ):
            raise ValueError(
                "runtime cannot be combined with execution_backend, "
                "rsync_syncer, or blobs"
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
        self.permissions = PermissionService()
        self.quotas = QuotaService(store=self.store)
        # Content-addressed store for gated-artifact bytes (and figures and
        # parachute objects). Local mode roots it next to the state DB; the
        # control composition injects an S3BlobStore (Phase 8). Same protocol,
        # same contract tests, so the rest of the app is blob-impl-blind.
        self.blobs = runtime.blobs
        self.execution_backend = runtime.execution_backend
        self.worker = runtime.worker
        self.feed_image_reader = runtime.feed_image_reader
        self.resource_artifact_reader = runtime.resource_artifact_reader
        self.resource_observer = runtime.resource_observer
        self.projects = ProjectService(store=self.store)
        self.claims = ClaimService(store=self.store)
        self.experiments = ExperimentService(
            store=self.store,
            blobs=self.blobs,
        )
        self.resources = ResourceService(
            store=self.store,
            permissions=self.permissions,
            blobs=self.blobs,
        )
        # One-time local upgrade: capture bytes for gated associations made
        # before byte capture existed (idempotent, skips present blobs).
        self._backfill_gated_blobs()
        self.syntheses = SynthesisService(
            store=self.store,
            claims=self.claims,
            experiment_writer=self.experiments,
            project_writer=self.projects,
            blobs=self.blobs,
        )
        self.reflections = ReflectionToolService(syntheses=self.syntheses)
        self.project_overview = ProjectOverviewService(
            store=self.store,
            projects=self.projects,
            syntheses=self.syntheses,
        )
        self.reviews = ReviewService(
            store=self.store,
            permissions=self.permissions,
            experiments=self.experiments,
            syntheses=self.syntheses,
            blobs=self.blobs,
        )
        self.sandboxes = SandboxService(
            store=self.store,
            sandbox_backend=self.execution_backend,
            worker=self.worker,
            activity=self.activity,
            experiments=self.experiments,
            # Per-sandbox management keys (plan Phase 5): control-plane
            # custody — local mode roots them under .research_plugin/ beside
            # the rest of the control state.
            mgmt_keys=runtime.mgmt_keys,
            metrics_archive=self.worker.metrics_archive,
            lease_client_id=self.worker.client_id(),
            # Decision 7's one shared blob store also holds parachute objects.
            blobs=self.blobs,
            quotas=self.quotas,
            # Split mode (Phase 8): the control composition injects an
            # HttpTaskChannel so control enqueues data-plane work to the daemon
            # over HTTP. None ⇒ the synchronous in-process channel (local mode).
            task_channel=(
                task_channel if task_channel is not None else runtime.task_channel
            ),
        )
        self.workflow = WorkflowService(
            store=self.store,
            experiments=self.experiments,
            reviews=self.reviews,
            sandboxes=self.sandboxes,
            syntheses=self.syntheses,
        )
        # Feed (Feed_PRD.md) is a self-contained module: it owns its schema,
        # tools, HTTP routes, and UI, and nothing in the research workflow
        # depends on it. Constructed here purely as a composition-root wiring.
        self.feed = FeedService(
            store=self.store,
            blobs=self.blobs,
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
                resource_register_file=self.register_resource_file,
                resource_associate=self.associate_resource,
                reviews=self.reviews,
                sandboxes=self.sandboxes,
                feed=self.feed,
                feed_post=self.post_feed,
            ),
            permissions=self.permissions,
            activity=self.activity,
            tool_calls=self.tool_calls,
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
            return {"synced": resources, "count": len(resources)}
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

    def post_feed(
        self,
        *,
        handle: str,
        text: str,
        image_path: str | None = None,
        url: str | None = None,
        ref: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if image_path:
            self.feed.validate_post_intent(
                handle=handle,
                text=text,
                ref=ref,
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
            project_id=project_id,
        )

    def shutdown(self) -> None:
        """Best-effort: stop background provisioning jobs and the sync poller."""
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
