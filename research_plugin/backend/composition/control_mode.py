"""Control-plane (cloud) composition (cloud plan Phase 8, §3.4).

Builds the multi-tenant control plane: record services + workflow + reviews +
sandbox lifecycle/provisioner/reaper + blob store + quotas.
Store from build_state_store (Postgres in hosted/no-repo-root control; SQLite
only when an explicit dev/test repo_root is supplied). Blob store from
build_blob_store. NO DataPlaneWorker rsync runs here — the cloud never touches
a user checkout; data-plane tool calls are
routed to the daemon by the proxy. The control plane enqueues data-plane work to
the daemon via the HttpTaskQueue and serves the daemon's task long-poll over
HTTP.

Provider creds resolve here (platform-owned keys, fixed decision 3). The cloud
NEVER dials a user machine: every cloud→daemon signal is a daemon-initiated
long-poll task.

Cloud reaper crash recovery (pulled into Phase 8 by the plan, risk 6): on
startup the control app scans tenant sandbox rows for running/provisioning and
resumes the reaper — a control restart must never leave billing VMs unreaped.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ..config import (
    ALLOWED_ORIGINS_ENV_VAR,
    BLOB_BUCKET_ENV_VAR,
    CONTROL_RESTRICT_CORS_ENV_VAR,
    DB_URL_ENV_VAR,
    MGMT_KEY_PATH_ENV_VAR,
    build_blob_store,
    build_object_store,
    build_state_store,
    resolve_blob_bucket,
    resolve_db_url,
    resolve_allowed_origins,
    resolve_mgmt_key_path,
    resolve_mgmt_public_key,
)
from ..control.control_app import ControlApp
from ..dataplane.http_channel import HttpTaskChannel, HttpTaskQueue
from ..env import env_bool, env_float
from ..execution import build_sandbox_backend
from ..transport.http_api import create_fastapi_app
from ..transport.http_policy import HttpSurfacePolicy
from ..services.cleanup import CleanupService
from ..services.mlflow_tracking import CentralMlflowService
from ..storage.service import StorageLedgerService
from ..state.managed_mgmt_keys import MountedMgmtKeyStore
from ..utils import ValidationError


CONTROL_COMPAT_REPO_ROOT = Path("/var/empty/research-plugin-control")
LOGGER = logging.getLogger(__name__)


class ControlPlaneServer:
    """A running control-plane app plus its daemon task queue and FastAPI app.

    Holds the app (record services), the HttpTaskQueue (the cloud→daemon task
    channel), and the FastAPI app that serves /mcp/* + /api/* + the daemon
    task/sync-target endpoints. ``fastapi_app`` is what uvicorn serves.
    """

    def __init__(
        self,
        *,
        app: ControlApp,
        task_queue: HttpTaskQueue,
        cleanup: CleanupService,
        fastapi_app: FastAPI,
    ) -> None:
        self.app = app
        self.task_queue = task_queue
        # The cloud cleanup sweeps (plan Phase 9). Built but NOT scheduled here —
        # scheduling is a documented seam (a managed cron / sidecar tick calls
        # ``cleanup.run_all(now=...)``). The reaper thread that IS owned lives in
        # SandboxDaemons; this is the broader periodic housekeeping.
        self.cleanup = cleanup
        self.fastapi_app = fastapi_app

    def shutdown(self) -> None:
        self.app.shutdown()


def build_control_app(
    *,
    repo_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    execution_backend: Any | None = None,
) -> tuple[ControlApp, HttpTaskQueue]:
    """Build the control-plane app and its daemon task queue.

    ``repo_root`` is an explicit dev/test staging dir for SQLite/blob defaults;
    production omits it and must provide DB_URL + BLOB_BUCKET + a mounted
    management key. The compatibility ``repo_root`` on that production path is
    a stable sentinel, not a created checkout or temp dir. ``execution_backend``
    lets the crash-recovery test inject a reaper-capable fake backend.
    """
    staging = _control_repo_root(repo_root=repo_root, env=env)
    db_path = staging / ".research_plugin" / "state.sqlite"
    store = build_state_store(db_path=db_path, env=env)
    blobs = build_blob_store(default_root=staging / ".research_plugin" / "blobs", env=env)
    objects = build_object_store(default_root=staging / ".research_plugin", env=env)
    storage = StorageLedgerService(store=store, objects=objects)
    # The cloud→daemon task channel: control enqueues, the daemon long-polls.
    # A bounded result wait keeps control lifecycle calls from blocking on a
    # missing daemon.
    result_timeout = env_float(
        "RESEARCH_PLUGIN_TASK_RESULT_TIMEOUT", None, 30.0, env=env, strict=True
    )
    task_queue = HttpTaskQueue()
    task_channel = HttpTaskChannel(queue=task_queue, result_timeout_seconds=result_timeout)
    if execution_backend is None:
        execution_backend = build_sandbox_backend(repo_root=staging)
    app = ControlApp(
        repo_root=staging,
        store=store,
        blobs=blobs,
        storage=storage,
        task_channel=task_channel,
        execution_backend=execution_backend,
        mgmt_keys=_build_mgmt_key_store(env=env),
        mlflow_tracking=CentralMlflowService.from_env(env),
    )
    # Cloud reaper crash recovery (plan Phase 8, risk 6): a control restart with
    # live VMs must re-acquire reaping. SandboxService already started the
    # reaper thread; this resumes any reconcile/reap work for rows left running.
    _resume_active_sandboxes(app=app)
    return app, task_queue


def build_control_server(
    *,
    repo_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    allowed_origins: list[str] | None = None,
) -> ControlPlaneServer:
    """Build the control-plane FastAPI server (daemon endpoints on)."""
    app, task_queue = build_control_app(repo_root=repo_root, env=env)
    origins = (
        resolve_allowed_origins(env) if allowed_origins is None else allowed_origins
    )
    surface = _control_http_surface(env=env)
    if surface.restrict_cors and not origins:
        LOGGER.warning(
            "%s is empty; browser clients will be blocked by hosted-control CORS",
            ALLOWED_ORIGINS_ENV_VAR,
        )
    cleanup = CleanupService(sandboxes=app.sandboxes, blobs=app.blobs, storage=app.storage)
    fastapi_app = create_fastapi_app(
        app=app,
        allowed_origins=origins,
        task_queue=task_queue,
        cleanup=cleanup,
        surface_policy=surface,
    )
    return ControlPlaneServer(
        app=app,
        task_queue=task_queue,
        cleanup=cleanup,
        fastapi_app=fastapi_app,
    )


def _control_repo_root(
    *, repo_root: Path | None, env: Mapping[str, str] | None = None
) -> Path:
    if repo_root is not None:
        return repo_root
    missing = []
    if not resolve_db_url(env):
        missing.append(DB_URL_ENV_VAR)
    if not resolve_blob_bucket(env):
        missing.append(BLOB_BUCKET_ENV_VAR)
    if not resolve_mgmt_key_path(env):
        missing.append(MGMT_KEY_PATH_ENV_VAR)
    if missing:
        raise ValidationError(
            "control mode without repo_root requires durable control-plane "
            f"configuration: {', '.join(missing)}",
            details={"missing": missing},
        )
    return CONTROL_COMPAT_REPO_ROOT


def _control_http_surface(
    *, env: Mapping[str, str] | None = None
) -> HttpSurfacePolicy:
    return HttpSurfacePolicy.for_surface(
        restrict_cors=env_bool(CONTROL_RESTRICT_CORS_ENV_VAR, True, env=env),
        hosted_control=True,
        expose_local_data_plane=False,
    )


def _build_mgmt_key_store(*, env: Mapping[str, str] | None = None):
    key_path = resolve_mgmt_key_path(env)
    public_key = resolve_mgmt_public_key(env)
    if not key_path:
        raise ValidationError(
            "RESEARCH_PLUGIN_MGMT_KEY_PATH is required in control mode; "
            "mount an externally managed management key"
        )
    return MountedMgmtKeyStore(
        private_key_path=Path(key_path),
        public_key=public_key,
    )


def _resume_active_sandboxes(*, app: ControlApp) -> None:
    """Reconcile rows left running/provisioning after a control restart.

    Mirror of ProjectRouter._resume_active_sandbox_projects for the cloud: the
    reaper thread is already running (SandboxService.__init__ started it); a
    one-shot reconcile pass on startup makes the resumed reaper truthful about
    rows that may have expired while the control plane was down. Best-effort —
    a reconcile failure must not block startup or the reaper.
    """
    try:
        running = app.sandboxes.registry.list_running_rows()
        for row in running:
            try:
                app.sandboxes.provisioner.reconcile(row=row)
            except Exception:  # noqa: BLE001 — per-row best-effort
                pass
        if running:
            # Kick the resumed reaper once so anything already past its deadline
            # is reaped promptly instead of waiting a full interval. Off-thread:
            # startup must not block on cleanup. The reaper thread the
            # SandboxService already started also catches it on its next tick.
            import threading

            threading.Thread(
                target=_safe_reap, args=(app,), name="control-recovery-reap", daemon=True
            ).start()
    except Exception:  # noqa: BLE001 — startup must not hinge on recovery
        pass


def _safe_reap(app: ControlApp) -> None:
    try:
        app.sandboxes.reap_expired()
    except Exception:  # noqa: BLE001 — the reaper must never die
        pass
