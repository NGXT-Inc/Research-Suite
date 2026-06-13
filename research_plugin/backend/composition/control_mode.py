"""Control-plane (cloud) composition (cloud plan Phase 8, §3.4).

Builds the multi-tenant control plane: record services + workflow + reviews +
sandbox lifecycle/provisioner/reaper + blob store + leases + quotas + auth.
Store from build_state_store (Postgres when RESEARCH_PLUGIN_DB_URL is set, else
SQLite — documented: a control plane with no DB runs on SQLite, fine for dev,
not for multi-tenant production). Blob store from build_blob_store (a bucket is
required for a real deployment so the parachute presign is a reachable HTTPS
PUT). NO DataPlaneWorker rsync runs here — the cloud never touches a user
checkout; data-plane tool calls are routed to the daemon by the proxy. The
control plane enqueues data-plane work to the daemon via the HttpTaskQueue and
serves the daemon's task long-poll + sync-target poll over HTTP.

Provider creds resolve here (platform-owned keys, fixed decision 3). The cloud
NEVER dials a user machine: every cloud→daemon signal is a daemon-initiated
long-poll task.

Cloud reaper crash recovery (pulled into Phase 8 by the plan, risk 6): on
startup the control app scans tenant sandbox rows for running/provisioning and
resumes the reaper — a control restart must never leave billing VMs unreaped.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ..app import ResearchPluginApp
from ..config import build_blob_store, build_state_store
from ..dataplane.http_channel import HttpTaskChannel, HttpTaskQueue
from ..http_api import create_fastapi_app
from ..services.identity import AuthService


class ControlPlaneServer:
    """A running control-plane app plus its daemon task queue and FastAPI app.

    Holds the app (record services), the HttpTaskQueue (the cloud→daemon task
    channel), and the FastAPI app that serves /mcp/* + /api/* + the daemon
    task/sync-target endpoints. ``fastapi_app`` is what uvicorn serves.
    """

    def __init__(
        self,
        *,
        app: ResearchPluginApp,
        task_queue: HttpTaskQueue,
        auth: AuthService,
        fastapi_app: FastAPI,
    ) -> None:
        self.app = app
        self.task_queue = task_queue
        self.auth = auth
        self.fastapi_app = fastapi_app

    def shutdown(self) -> None:
        self.app.shutdown()


def build_control_app(
    *,
    repo_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    execution_backend: Any | None = None,
) -> tuple[ResearchPluginApp, HttpTaskQueue, AuthService]:
    """Build the control-plane app, its daemon task queue, and AuthService.

    ``repo_root`` is a throwaway control-side staging dir for the SQLite/blob
    defaults and telemetry sinks; the control plane never holds a USER checkout
    (it serves no repo_root over the edge — the proxy strips it). Tests pass an
    explicit dir; production sets DB_URL + BLOB_BUCKET so the local paths are
    unused. ``execution_backend`` lets the crash-recovery test inject a
    reaper-capable fake backend.
    """
    staging = repo_root or Path(tempfile.mkdtemp(prefix="rp-control-"))
    db_path = staging / ".research_plugin" / "state.sqlite"
    store = build_state_store(db_path=db_path, env=env)
    blobs = build_blob_store(default_root=staging / ".research_plugin" / "blobs", env=env)
    # The cloud→daemon task channel: control enqueues, the daemon long-polls.
    # A bounded result wait so a reaper final_pull falls through to the expiry
    # parachute promptly when the daemon is unreachable (billing protection
    # beats data recovery) instead of blocking the reaper thread.
    import os

    result_timeout = float(
        os.environ.get("RESEARCH_PLUGIN_TASK_RESULT_TIMEOUT", "30") or "30"
    )
    task_queue = HttpTaskQueue()
    task_channel = HttpTaskChannel(queue=task_queue, result_timeout_seconds=result_timeout)
    app = ResearchPluginApp(
        repo_root=staging,
        db_path=db_path,
        store=store,
        blobs=blobs,
        task_channel=task_channel,
        execution_backend=execution_backend,
    )
    auth = AuthService(store=app.store)
    # Cloud reaper crash recovery (plan Phase 8, risk 6): a control restart with
    # live VMs must re-acquire reaping. SandboxService already started the
    # reaper thread; this resumes any reconcile/reap work for rows left running.
    _resume_active_sandboxes(app=app)
    return app, task_queue, auth


def build_control_server(
    *,
    repo_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    allowed_origins: list[str] | None = None,
) -> ControlPlaneServer:
    """Build the control-plane FastAPI server (auth ON, daemon endpoints on)."""
    app, task_queue, auth = build_control_app(repo_root=repo_root, env=env)
    fastapi_app = create_fastapi_app(
        app=app,
        auth=auth,
        allowed_origins=allowed_origins,
        task_queue=task_queue,
        sync_targets_source=app.sandboxes.control_view,
    )
    return ControlPlaneServer(
        app=app, task_queue=task_queue, auth=auth, fastapi_app=fastapi_app
    )


def _resume_active_sandboxes(*, app: ResearchPluginApp) -> None:
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
            # a final_pull task blocks on the (long-poll) daemon, and startup
            # must not block on that. The reaper thread the SandboxService
            # already started also catches it on its next tick.
            import threading

            threading.Thread(
                target=_safe_reap, args=(app,), name="control-recovery-reap", daemon=True
            ).start()
    except Exception:  # noqa: BLE001 — startup must not hinge on recovery
        pass


def _safe_reap(app: ResearchPluginApp) -> None:
    try:
        app.sandboxes.reap_expired()
    except Exception:  # noqa: BLE001 — the reaper must never die
        pass
