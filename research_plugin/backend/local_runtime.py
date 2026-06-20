"""Local runtime wiring for single-process app mode."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .dataplane import LocalDataPlaneWorker
from .dataplane.feed_images import LocalFeedImageReader
from .dataplane.resource_artifacts import LocalResourceArtifactReader
from .dataplane.resource_observer import LocalResourceObserver
from .dataplane.tasks import InProcessTaskChannel
from .execution import SandboxBackend, build_sandbox_backend
from .execution.ssh_rsync import SshRsyncSyncer
from .services.sandbox_mgmt_keys import LocalMgmtKeyStore
from .state import ActivityLogger, ToolCallStore
from .state.blobs import BlobStore, LocalDirBlobStore
from .workspace import LocalWorkspace


@dataclass(frozen=True)
class LocalRuntime:
    workspace: LocalWorkspace
    activity: ActivityLogger
    tool_calls: ToolCallStore
    blobs: BlobStore
    execution_backend: SandboxBackend
    worker: LocalDataPlaneWorker
    feed_image_reader: LocalFeedImageReader
    resource_artifact_reader: LocalResourceArtifactReader
    resource_observer: LocalResourceObserver
    task_channel: InProcessTaskChannel
    mgmt_keys: LocalMgmtKeyStore


def build_local_runtime(
    *,
    repo_root: Path,
    execution_backend: SandboxBackend | None = None,
    rsync_syncer: SshRsyncSyncer | None = None,
    blobs: BlobStore | None = None,
) -> LocalRuntime:
    """Build local filesystem, telemetry, backend, and worker collaborators."""
    workspace = LocalWorkspace(repo_root=repo_root)
    activity = ActivityLogger(repo_root=workspace.repo_root)

    def _activity_hook(event_type: str, payload: dict[str, object]) -> None:
        try:
            activity.emit(event_type=event_type, payload=payload)
        except Exception:  # noqa: BLE001
            pass

    if execution_backend is None:
        execution_backend = build_sandbox_backend(
            repo_root=workspace.repo_root,
            activity=_activity_hook,
        )
    worker = LocalDataPlaneWorker(
        workspace=workspace,
        backend=execution_backend,
        rsync_syncer=rsync_syncer,
    )
    return LocalRuntime(
        workspace=workspace,
        activity=activity,
        tool_calls=ToolCallStore(
            db_path=workspace.research_dir / "tool_calls.sqlite"
        ),
        blobs=blobs if blobs is not None else LocalDirBlobStore(
            root=workspace.research_dir / "blobs"
        ),
        execution_backend=execution_backend,
        worker=worker,
        feed_image_reader=LocalFeedImageReader(repo_root=workspace.repo_root),
        resource_artifact_reader=LocalResourceArtifactReader(repo_root=workspace.repo_root),
        resource_observer=LocalResourceObserver(repo_root=workspace.repo_root),
        task_channel=InProcessTaskChannel(worker=worker),
        mgmt_keys=LocalMgmtKeyStore(root=workspace.research_dir / "mgmt_keys"),
    )
