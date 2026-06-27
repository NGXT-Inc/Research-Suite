"""Central mode/config resolution for the backend.

The cloud split (docs/CONTROL_DATA_PLANE_SPLIT.md) gives the backend three
process roles selected by ``RESEARCH_PLUGIN_MODE``:

- ``local``  — today's topology: one process binds the control plane and the
  data plane in-process (default, and the only mode implemented so far).
- ``control`` — cloud control plane (multi-tenant records, gates, lifecycle).
- ``daemon`` — slim local data-plane daemon (rsync, keys, file observation).

Mode resolution is fail-fast: an unknown value refuses to start rather than
silently running in the wrong topology. All later config (DB URLs, blob
backends, control URLs) hangs off this module so there is exactly one place
that decides what a process is.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from research_plugin_shared.client_config import (
    CLIENT_CONFIG_ENV_VAR,
    CONTROL_URL_ENV_VAR,
    DAEMON_STATE_DIR_ENV_VAR,
    read_client_config,
    resolve_client_config_path,
)

from .utils import ValidationError

if TYPE_CHECKING:  # the store import stays lazy at runtime (see build_state_store)
    from .state import BaseStateStore


MODE_ENV_VAR = "RESEARCH_PLUGIN_MODE"

# Record-store selection (cloud plan Phase 6). Absent ⇒ the SQLite default
# (local mode, today's behavior, byte-identical). A postgres:// URL selects
# the Postgres dialect — used by tests and the control profile.
DB_URL_ENV_VAR = "RESEARCH_PLUGIN_DB_URL"

# Split-transport config (cloud plan Phase 8, §3.4). The daemon dials the cloud
# at CONTROL_URL. The cloud never dials in. The blob bucket/dir selects the
# BlobStore impl per mode.
BLOB_DIR_ENV_VAR = "RESEARCH_PLUGIN_BLOB_DIR"
BLOB_BUCKET_ENV_VAR = "RESEARCH_PLUGIN_BLOB_BUCKET"
STORAGE_PROVIDER_ENV_VAR = "RESEARCH_PLUGIN_STORAGE_PROVIDER"
STORAGE_BUCKET_ENV_VAR = "RESEARCH_PLUGIN_STORAGE_BUCKET"
STORAGE_ENDPOINT_URL_ENV_VAR = "RESEARCH_PLUGIN_STORAGE_ENDPOINT_URL"
STORAGE_REGION_ENV_VAR = "RESEARCH_PLUGIN_STORAGE_REGION"
STORAGE_ACCESS_KEY_ID_ENV_VAR = "RESEARCH_PLUGIN_STORAGE_ACCESS_KEY_ID"
STORAGE_SECRET_ACCESS_KEY_ENV_VAR = "RESEARCH_PLUGIN_STORAGE_SECRET_ACCESS_KEY"
MGMT_KEY_PATH_ENV_VAR = "RESEARCH_PLUGIN_MGMT_KEY_PATH"
MGMT_PUBLIC_KEY_ENV_VAR = "RESEARCH_PLUGIN_MGMT_PUBLIC_KEY"
ALLOWED_ORIGINS_ENV_VAR = "RESEARCH_PLUGIN_ALLOWED_ORIGINS"
CONTROL_RESTRICT_CORS_ENV_VAR = "RESEARCH_PLUGIN_CONTROL_RESTRICT_CORS"
MLFLOW_MODE_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_MODE"
MLFLOW_TRACKING_URI_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_TRACKING_URI"
MLFLOW_SERVER_URI_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_SERVER_URI"
MLFLOW_DASHBOARD_URL_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_DASHBOARD_URL"

_POSTGRES_URL_PREFIXES = ("postgres://", "postgresql://")


class Mode(str, Enum):
    """The process role (cloud plan §1.1). ``local`` is the default forever."""

    LOCAL = "local"
    CONTROL = "control"
    DAEMON = "daemon"


def resolve_mode(env: Mapping[str, str] | None = None) -> Mode:
    """Resolve the process mode from the environment, failing fast.

    All three modes are runnable as of Phase 8; an unknown value still refuses
    to start rather than silently running the wrong topology. Mode-specific
    fail-fast validation (a daemon without a control URL, a control plane's DB)
    lives in the composition roots, not here, so this stays a pure parse.
    """
    source = env if env is not None else os.environ
    raw = (source.get(MODE_ENV_VAR) or "").strip().lower() or Mode.LOCAL.value
    try:
        return Mode(raw)
    except ValueError as exc:
        raise ValidationError(
            f"unknown RESEARCH_PLUGIN_MODE: {raw!r} "
            "(expected 'local', 'control', or 'daemon')",
            details={"mode": raw},
        ) from exc


def resolve_db_url(env: Mapping[str, str] | None = None) -> str | None:
    """The configured record-store URL, or None for the SQLite path default."""
    source = env if env is not None else os.environ
    return (source.get(DB_URL_ENV_VAR) or "").strip() or None


def resolve_daemon_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Machine-local daemon state root.

    This is not a research repo. It stores the daemon registry, loopback secret,
    pid/log files, sandbox keys, and other machine-local data-plane state. One
    daemon state root can hold many repo→project links.
    """
    source = env if env is not None else os.environ
    raw = (source.get(DAEMON_STATE_DIR_ENV_VAR) or "").strip()
    if raw:
        return Path(raw).expanduser()
    config = read_client_config(env)
    raw = (config.get("daemon_state_dir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return resolve_client_config_path(env).parent


def resolve_control_url(env: Mapping[str, str] | None = None) -> str | None:
    """The cloud control-plane URL the daemon dials, or None (plan §3.4)."""
    source = env if env is not None else os.environ
    raw = (source.get(CONTROL_URL_ENV_VAR) or "").strip()
    if not raw:
        raw = read_client_config(env).get("control_url", "")
    return raw.rstrip("/") or None


def resolve_blob_dir(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(BLOB_DIR_ENV_VAR) or "").strip() or None


def resolve_blob_bucket(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(BLOB_BUCKET_ENV_VAR) or "").strip() or None


def storage_feature_enabled(env: Mapping[str, str] | None = None) -> bool:
    return resolve_storage_provider(env) is not None


def resolve_storage_provider(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    raw = (source.get(STORAGE_PROVIDER_ENV_VAR) or "").strip().lower()
    if not raw:
        return None
    if raw != "s3":
        raise ValidationError(
            f"unknown {STORAGE_PROVIDER_ENV_VAR}: {raw!r} "
            "(expected 's3', or unset to disable storage)",
            details={"provider": raw},
        )
    return raw


def resolve_storage_bucket(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(STORAGE_BUCKET_ENV_VAR) or "").strip() or None


def resolve_storage_endpoint_url(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(STORAGE_ENDPOINT_URL_ENV_VAR) or "").strip() or None


def resolve_storage_region(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(STORAGE_REGION_ENV_VAR) or "").strip() or None


def resolve_storage_access_key_id(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (
        (source.get(STORAGE_ACCESS_KEY_ID_ENV_VAR) or "").strip()
        or (source.get("AWS_ACCESS_KEY_ID") or "").strip()
        or None
    )


def resolve_storage_secret_access_key(
    env: Mapping[str, str] | None = None,
) -> str | None:
    source = env if env is not None else os.environ
    return (
        (source.get(STORAGE_SECRET_ACCESS_KEY_ENV_VAR) or "").strip()
        or (source.get("AWS_SECRET_ACCESS_KEY") or "").strip()
        or None
    )


def resolve_mgmt_key_path(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(MGMT_KEY_PATH_ENV_VAR) or "").strip() or None


def resolve_mgmt_public_key(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(MGMT_PUBLIC_KEY_ENV_VAR) or "").strip() or None


def resolve_allowed_origins(env: Mapping[str, str] | None = None) -> list[str]:
    """Hosted-control CORS allowlist from a comma-separated env var."""
    source = env if env is not None else os.environ
    raw = (source.get(ALLOWED_ORIGINS_ENV_VAR) or "").strip()
    if not raw:
        return []
    origins: list[str] = []
    for part in raw.split(","):
        origin = part.strip().rstrip("/")
        if not origin:
            continue
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValidationError(
                f"invalid {ALLOWED_ORIGINS_ENV_VAR} origin: {origin!r} "
                "(expected http:// or https:// origin with no path, query, or fragment)",
                details={"origin": origin},
            )
        origins.append(origin)
    return origins


def resolve_mlflow_mode(env: Mapping[str, str] | None = None) -> str:
    """Centralized MLflow mode, or '' when MLflow is not configured yet."""
    source = env if env is not None else os.environ
    raw = (source.get(MLFLOW_MODE_ENV_VAR) or "").strip().lower()
    if not raw:
        return ""
    if raw not in {"managed", "external"}:
        raise ValidationError(
            f"unknown {MLFLOW_MODE_ENV_VAR}: {raw!r} "
            "(expected 'managed' or 'external')",
            details={"mode": raw},
        )
    return raw


def resolve_mlflow_tracking_uri(env: Mapping[str, str] | None = None) -> str:
    """The backend-owned MLflow tracking URI exposed to agents."""
    source = env if env is not None else os.environ
    return (source.get(MLFLOW_TRACKING_URI_ENV_VAR) or "").strip().rstrip("/")


def resolve_mlflow_server_uri(env: Mapping[str, str] | None = None) -> str:
    """Optional backend-internal MLflow URI for control-plane reads."""
    source = env if env is not None else os.environ
    return (source.get(MLFLOW_SERVER_URI_ENV_VAR) or "").strip().rstrip("/")


def resolve_mlflow_dashboard_url(env: Mapping[str, str] | None = None) -> str:
    """Optional human-facing MLflow dashboard URL; defaults to tracking URI."""
    source = env if env is not None else os.environ
    return (source.get(MLFLOW_DASHBOARD_URL_ENV_VAR) or "").strip().rstrip("/")


def build_blob_store(
    *, default_root: Path, env: Mapping[str, str] | None = None
):
    """The BlobStore the configuration selects (cloud plan Phase 8).

    A bucket name selects ``S3BlobStore`` (boto3 imported only on that branch,
    so local installs never need it); otherwise a ``LocalDirBlobStore`` rooted
    at the configured dir or ``default_root``. Same protocol + contract tests
    either way, so callers stay blob-impl-blind. The control profile MUST set a
    bucket so off-process uploads use reachable HTTPS URLs instead of local
    loopback tokens.
    """
    bucket = resolve_blob_bucket(env)
    if bucket:
        from .state.s3_blobs import S3BlobStore

        return S3BlobStore(bucket=bucket)
    from .state.blobs import LocalDirBlobStore

    root = resolve_blob_dir(env)
    return LocalDirBlobStore(root=Path(root) if root else default_root)


def build_object_store(
    *, default_root: Path, env: Mapping[str, str] | None = None
):
    """The heavy-object store selected by storage env config.

    Unset disables storage entirely. ``s3`` covers AWS S3, MinIO, and R2; R2 is
    just S3 with an endpoint URL. There is intentionally no in-process local
    provider: local/offline users should run an S3-compatible service such as
    MinIO and point this config at it.
    """
    provider = resolve_storage_provider(env)
    _ = default_root
    if provider is None:
        return None
    bucket = resolve_storage_bucket(env)
    if not bucket:
        raise ValidationError(
            f"{STORAGE_BUCKET_ENV_VAR} is required when {STORAGE_PROVIDER_ENV_VAR}=s3",
            details={"provider": provider},
        )
    from .storage.s3_object_store import S3CompatibleObjectStore

    return S3CompatibleObjectStore(
        bucket=bucket,
        endpoint_url=resolve_storage_endpoint_url(env),
        region_name=resolve_storage_region(env),
        access_key_id=resolve_storage_access_key_id(env),
        secret_access_key=resolve_storage_secret_access_key(env),
    )


def build_state_store(
    *, db_path: Path, env: Mapping[str, str] | None = None
) -> "BaseStateStore":
    """The record store the configuration selects, fail-fast like the mode.

    No URL keeps today's behavior exactly: the SQLite store at ``db_path``.
    A postgres:// URL selects the Postgres dialect (psycopg imported only on
    that branch, so local installs never need it). Any other scheme refuses
    to start rather than guessing a dialect.
    """
    url = resolve_db_url(env)
    if url is None:
        from .state import StateStore

        return StateStore(db_path=db_path)
    if url.startswith(_POSTGRES_URL_PREFIXES):
        from .state.dialects import PostgresStateStore

        return PostgresStateStore(dsn=url)
    raise ValidationError(
        f"unsupported {DB_URL_ENV_VAR} scheme: {url!r} "
        "(expected postgres:// or postgresql://, or unset for SQLite)",
        details={"db_url": url},
    )
