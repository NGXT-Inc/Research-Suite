"""Unified brain composition with local and hosted deployment presets.

The composition wires records, workflow, reviews, blobs, MLflow, quotas, and
sandbox lifecycle. Hosted/no-checkout control requires Postgres, a durable blob
store, and mounted management keys; local deployment selects SQLite and local
adapters. Checkout I/O never runs here: the stdio MCP proxy submits explicit
facts and bounded bytes, and the brain never dials a user machine.
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
    REQUIRE_AGENT_MLFLOW_ENV_VAR,
    REQUIRE_SANDBOX_BACKEND_ENV_VAR,
    resolve_blob_bucket,
    resolve_db_url,
    resolve_allowed_origins,
    resolve_mgmt_key_path,
    resolve_mgmt_public_key,
    resolve_ui_base_url,
)
from ..control.control_app import ControlApp
from ..control.control_runtime import ControlTaskChannel
from ..control.storage_quotas import StorageQuotaService
from ..env import env_bool
from ..execution import build_sandbox_backend
from ..transport.http_api import create_fastapi_app
from ..transport.http_policy import HttpSurfacePolicy
from ..services.auth import (
    SUPABASE_JWT_SECRET_ENV_VAR,
    SUPABASE_URL_ENV_VAR,
    SupabaseVerifier,
)
from ..services.cleanup import CleanupService
from ..mlflow import CentralMlflowService
from ..mlflow.config import MLFLOW_TRACKING_URI_ENV_VAR
from ..storage.service import StorageLedgerService
from ..sandbox.managed_mgmt_keys import MountedMgmtKeyStore
from ..sandbox.mgmt_keys import LocalMgmtKeyStore
from ..utils import ValidationError


CONTROL_COMPAT_REPO_ROOT = Path("/var/empty/merv-control")
LOCAL_BRAIN_STATE_DIR_ENV_VAR = "RESEARCH_PLUGIN_LOCAL_STATE_DIR"
LOGGER = logging.getLogger(__name__)
_UNSET = object()


class ControlPlaneServer:
    """A running brain app plus its FastAPI surface.

    Holds the record/policy app, task channel, cleanup service, and FastAPI app
    that serves ``/mcp/*`` and ``/api/*``. Both deployment presets use it.
    ``fastapi_app`` is what uvicorn serves.
    """

    def __init__(
        self,
        *,
        app: ControlApp,
        task_channel: ControlTaskChannel,
        cleanup: CleanupService,
        fastapi_app: FastAPI,
    ) -> None:
        self.app = app
        self.task_channel = task_channel
        # Broader cleanup sweeps are built but NOT scheduled here — a managed
        # cron or sidecar tick calls ``cleanup.run_all(now=...)``. The owned
        # expiry reaper lives in SandboxService; this is broader housekeeping.
        self.cleanup = cleanup
        self.fastapi_app = fastapi_app

    def shutdown(self) -> None:
        self.app.shutdown()


def build_control_app(
    *,
    repo_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    execution_backend: Any | None = None,
    store: Any | None = None,
    blobs: Any | None = None,
    storage: Any = _UNSET,
    task_channel: Any | None = None,
    mgmt_keys: Any | None = None,
    mlflow_tracking: CentralMlflowService | None = None,
    local_deployment: bool = False,
) -> tuple[ControlApp, ControlTaskChannel]:
    """Build the unified brain app and its neutral task channel.

    ``repo_root`` is an explicit dev/test staging dir for SQLite/blob defaults;
    production omits it and must provide DB_URL + BLOB_BUCKET + a mounted
    management key. The compatibility ``repo_root`` on that production path is
    a stable sentinel, not a created checkout or temp dir. ``execution_backend``
    lets the crash-recovery test inject a reaper-capable fake backend.
    """
    staging = _control_repo_root(
        repo_root=repo_root, env=env, local_deployment=local_deployment
    )
    db_path = staging / ".research_plugin" / "state.sqlite"
    store = store if store is not None else build_state_store(db_path=db_path, env=env)
    blobs = (
        blobs
        if blobs is not None
        else build_blob_store(default_root=staging / ".research_plugin" / "blobs", env=env)
    )
    if storage is _UNSET:
        objects = build_object_store(default_root=staging / ".research_plugin", env=env)
        storage = (
            StorageLedgerService(
                store=store,
                objects=objects,
                blob_quotas=StorageQuotaService(),
            )
            if objects
            else None
        )
    task_channel = task_channel if task_channel is not None else ControlTaskChannel()
    if execution_backend is None:
        execution_backend = build_sandbox_backend(repo_root=staging)
    mlflow_tracking = (
        mlflow_tracking
        if mlflow_tracking is not None
        else CentralMlflowService.from_env(env)
    )
    _validate_agent_mlflow_requirement(mlflow_tracking=mlflow_tracking, env=env)
    _validate_sandbox_backend_requirement(execution_backend=execution_backend, env=env)
    app = ControlApp(
        repo_root=staging,
        store=store,
        blobs=blobs,
        storage=storage,
        task_channel=task_channel,
        execution_backend=execution_backend,
        mgmt_keys=(
            mgmt_keys
            if mgmt_keys is not None
            else _build_mgmt_key_store(
                env=env,
                local_root=staging if local_deployment else None,
            )
        ),
        mlflow_tracking=mlflow_tracking,
        # The brain holds provider lifecycle responsibility, so this composition
        # forces the expiry reaper on in both deployment presets.
        force_expiry_reaper=True,
    )
    # A brain restart with live VMs must re-acquire reaping. SandboxService has
    # already started the thread; this reconciles rows left running.
    _resume_active_sandboxes(app=app)
    return app, task_channel


def _validate_auth_requirement(
    *,
    auth: SupabaseVerifier | None,
) -> None:
    if auth is not None:
        return
    raise ValidationError(
        "hosted control requires Supabase authentication; set "
        f"{SUPABASE_URL_ENV_VAR} and {SUPABASE_JWT_SECRET_ENV_VAR} "
        "(shared with the RapidReview Supabase project)",
        details={"missing": [SUPABASE_URL_ENV_VAR, SUPABASE_JWT_SECRET_ENV_VAR]},
    )


def build_control_server(
    *,
    repo_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    allowed_origins: list[str] | None = None,
) -> ControlPlaneServer:
    """Build the hosted-control FastAPI brain."""
    auth = SupabaseVerifier.from_env(env)
    _validate_auth_requirement(auth=auth)
    app, task_channel = build_control_app(repo_root=repo_root, env=env)
    origins = (
        resolve_allowed_origins(env) if allowed_origins is None else allowed_origins
    )
    surface = _control_http_surface(env=env)
    if surface.restrict_cors and not origins:
        LOGGER.warning(
            "%s is empty; browser clients will be blocked by hosted-control CORS",
            ALLOWED_ORIGINS_ENV_VAR,
        )
    cleanup = CleanupService(
        sandboxes=app.sandboxes, blobs=app.blobs, storage=app.storage
    )
    fastapi_app = create_fastapi_app(
        app=app,
        allowed_origins=origins,
        cleanup=cleanup,
        surface_policy=surface,
        auth=auth,
        ui_base_url=resolve_ui_base_url(env),
    )
    return ControlPlaneServer(
        app=app,
        task_channel=task_channel,
        cleanup=cleanup,
        fastapi_app=fastapi_app,
    )


def build_local_server(
    *,
    state_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    allowed_origins: list[str] | None = None,
    execution_backend: Any | None = None,
    store: Any | None = None,
    blobs: Any | None = None,
    storage: Any = _UNSET,
    task_channel: Any | None = None,
    mgmt_keys: Any | None = None,
    mlflow_tracking: CentralMlflowService | None = None,
) -> ControlPlaneServer:
    """Build the localhost brain using the same ControlApp composition."""
    root = _local_brain_root(state_dir=state_dir, env=env)
    app, task_channel = build_control_app(
        repo_root=root,
        env=env,
        execution_backend=execution_backend,
        store=store,
        blobs=blobs,
        storage=storage,
        task_channel=task_channel,
        mgmt_keys=mgmt_keys,
        mlflow_tracking=mlflow_tracking,
        local_deployment=True,
    )
    cleanup = CleanupService(sandboxes=app.sandboxes, blobs=app.blobs, storage=app.storage)
    fastapi_app = create_fastapi_app(
        app=app,
        allowed_origins=allowed_origins or [],
        cleanup=cleanup,
        surface_policy=_local_http_surface(),
    )
    return ControlPlaneServer(
        app=app,
        task_channel=task_channel,
        cleanup=cleanup,
        fastapi_app=fastapi_app,
    )


def _control_repo_root(
    *,
    repo_root: Path | None,
    env: Mapping[str, str] | None = None,
    local_deployment: bool = False,
) -> Path:
    if repo_root is not None:
        return repo_root
    if local_deployment:
        return _local_brain_root(state_dir=None, env=env)
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


def _local_brain_root(
    *, state_dir: Path | None, env: Mapping[str, str] | None = None
) -> Path:
    if state_dir is not None:
        return state_dir.expanduser().resolve()
    source = env if env is not None else None
    raw = ((source or {}).get(LOCAL_BRAIN_STATE_DIR_ENV_VAR) or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home().joinpath(".research_plugin", "brain").expanduser().resolve()


def _control_http_surface(
    *, env: Mapping[str, str] | None = None
) -> HttpSurfacePolicy:
    return HttpSurfacePolicy.for_surface(
        restrict_cors=env_bool(CONTROL_RESTRICT_CORS_ENV_VAR, True, env=env),
        hosted_control=True,
    )


def _local_http_surface() -> HttpSurfacePolicy:
    return HttpSurfacePolicy.for_surface(
        restrict_cors=False,
        hosted_control=False,
    )


def _build_mgmt_key_store(
    *,
    env: Mapping[str, str] | None = None,
    local_root: Path | None = None,
):
    if local_root is not None:
        return LocalMgmtKeyStore(root=local_root / ".research_plugin" / "mgmt_keys")
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


def _validate_agent_mlflow_requirement(
    *,
    mlflow_tracking: CentralMlflowService,
    env: Mapping[str, str] | None = None,
) -> None:
    if not env_bool(REQUIRE_AGENT_MLFLOW_ENV_VAR, False, env=env):
        return
    if mlflow_tracking.tracking_uri:
        return
    raise ValidationError(
        f"{REQUIRE_AGENT_MLFLOW_ENV_VAR}=1 requires "
        f"{MLFLOW_TRACKING_URI_ENV_VAR}; set it to the public, run-reachable "
        "MLflow URL agents should receive as MLFLOW_TRACKING_URI, or disable "
        "the requirement for an intentional read-only/server-only deployment.",
        details={"missing": [MLFLOW_TRACKING_URI_ENV_VAR]},
    )


def _validate_sandbox_backend_requirement(
    *,
    execution_backend: Any,
    env: Mapping[str, str] | None = None,
) -> None:
    if not env_bool(REQUIRE_SANDBOX_BACKEND_ENV_VAR, False, env=env):
        return
    health = dict(execution_backend.health())
    if health.get("ok"):
        return
    backend = str(
        health.get("backend") or health.get("name") or health.get("provider") or "unknown"
    )
    error = str(health.get("error") or "sandbox backend health check failed")
    raise ValidationError(
        f"{REQUIRE_SANDBOX_BACKEND_ENV_VAR}=1 requires a healthy sandbox backend "
        f"before control startup; {backend} reported: {error}",
        details={"backend": backend, "error": error},
    )


def _resume_active_sandboxes(*, app: ControlApp) -> None:
    """Reconcile rows left running/provisioning after a control restart.

    The reaper thread is already running (SandboxService.__init__ started it);
    a one-shot reconcile pass on startup makes the resumed reaper truthful
    about rows that may have expired while the control plane was down.
    Best-effort — a reconcile failure must not block startup or the reaper.
    """
    try:
        had_running = bool(app.sandboxes.registry.list_running_rows())
        app.sandboxes.reconcile_running_rows()
        if had_running:
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
