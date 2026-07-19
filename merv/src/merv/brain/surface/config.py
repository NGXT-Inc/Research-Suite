"""Central deployment-preset and adapter configuration for the brain.

``local`` and ``control`` use the same brain composition. The preset selects
loopback/small-store defaults or hosted durable adapters; the stdio MCP proxy
remains the checkout-local data plane in both cases. Unknown preset values fail
at startup.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from merv.shared.client_config import (
    CLIENT_CONFIG_ENV_VAR,
    CONTROL_URL_ENV_VAR,
    DAEMON_STATE_DIR_ENV_VAR,
    read_client_config,
    resolve_client_config_path,
)

from ..kernel.env import env_value
from ..kernel.utils import ValidationError

if TYPE_CHECKING:  # the store import stays lazy at runtime (see build_state_store)
    from ..kernel.state import BaseStateStore


MODE_ENV_VAR = "MERV_MODE"

# Record-store selection. Absent means the SQLite default used by the local
# preset. A postgres:// or postgresql:// URL selects the hosted Postgres dialect.
DB_URL_ENV_VAR = "MERV_DB_URL"

# Split-transport config. The MCP proxy sends local data-plane submissions to
# CONTROL_URL. The brain never dials a user machine. The blob bucket/dir
# selects the BlobStore impl per mode.
BLOB_DIR_ENV_VAR = "MERV_BLOB_DIR"
BLOB_BUCKET_ENV_VAR = "MERV_BLOB_BUCKET"
STORAGE_PROVIDER_ENV_VAR = "MERV_STORAGE_PROVIDER"
STORAGE_BUCKET_ENV_VAR = "MERV_STORAGE_BUCKET"
STORAGE_ENDPOINT_URL_ENV_VAR = "MERV_STORAGE_ENDPOINT_URL"
STORAGE_REGION_ENV_VAR = "MERV_STORAGE_REGION"
STORAGE_ACCESS_KEY_ID_ENV_VAR = "MERV_STORAGE_ACCESS_KEY_ID"
STORAGE_SECRET_ACCESS_KEY_ENV_VAR = "MERV_STORAGE_SECRET_ACCESS_KEY"
MGMT_KEY_PATH_ENV_VAR = "MERV_MGMT_KEY_PATH"
MGMT_PUBLIC_KEY_ENV_VAR = "MERV_MGMT_PUBLIC_KEY"
ALLOWED_ORIGINS_ENV_VAR = "MERV_ALLOWED_ORIGINS"
CONTROL_RESTRICT_CORS_ENV_VAR = "MERV_CONTROL_RESTRICT_CORS"
# Where the device-flow sign-in page lives. Unlike a CORS origin this may
# carry a path (e.g. https://rapidreview.io/merv) for path-mounted UIs.
UI_BASE_URL_ENV_VAR = "MERV_UI_BASE_URL"
# MLflow-extension env config (MLFLOW_MODE/TRACKING_URI/SERVER_URI/DASHBOARD)
# lives in src/merv/brain/mlflow/config.py — the extension owns its own knobs.
# The enforcement knob below is composition policy, so it stays here.
REQUIRE_AGENT_MLFLOW_ENV_VAR = "MERV_REQUIRE_AGENT_MLFLOW"
REQUIRE_SANDBOX_BACKEND_ENV_VAR = "MERV_REQUIRE_SANDBOX_BACKEND"
# Supabase auth knobs (SUPABASE_URL/JWT_SECRET/...) live in services/auth.py —
# the extension owns them; this composition knob makes control mode fail fast
# when they are missing instead of booting an open surface.
REQUIRE_AUTH_ENV_VAR = "MERV_REQUIRE_AUTH"

_POSTGRES_URL_PREFIXES = ("postgres://", "postgresql://")


class Mode(str, Enum):
    """Brain deployment preset. ``local`` is the default."""

    LOCAL = "local"
    CONTROL = "control"


def resolve_mode(env: Mapping[str, str] | None = None) -> Mode:
    """Resolve the process mode from the environment, failing fast.

    Unknown values refuse to start rather than silently running the wrong
    topology. Mode-specific fail-fast validation lives in the composition
    roots, not here, so this stays a pure parse.
    """
    raw = (env_value(MODE_ENV_VAR, env=env) or Mode.LOCAL.value).lower()
    try:
        return Mode(raw)
    except ValueError as exc:
        raise ValidationError(
            f"unknown {MODE_ENV_VAR}: {raw!r} "
            "(expected 'local' or 'control')",
            details={"mode": raw},
        ) from exc


def resolve_db_url(env: Mapping[str, str] | None = None) -> str | None:
    """The configured record-store URL, or None for the SQLite path default."""
    return env_value(DB_URL_ENV_VAR, env=env)


def resolve_daemon_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Backward-compatible machine-local client state root.

    This is not a research repo. The name is retained because existing client
    configs store repo→project links under ``daemon_state_dir``.
    """
    raw = env_value(DAEMON_STATE_DIR_ENV_VAR, env=env) or ""
    if raw:
        return Path(raw).expanduser()
    config = read_client_config(env)
    raw = (config.get("daemon_state_dir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return resolve_client_config_path(env).parent


def resolve_control_url(env: Mapping[str, str] | None = None) -> str | None:
    """The configured brain URL (hosted or localhost), or None."""
    raw = env_value(CONTROL_URL_ENV_VAR, env=env) or ""
    if not raw:
        raw = read_client_config(env).get("control_url", "")
    return raw.rstrip("/") or None


def resolve_blob_dir(env: Mapping[str, str] | None = None) -> str | None:
    return env_value(BLOB_DIR_ENV_VAR, env=env)


def resolve_blob_bucket(env: Mapping[str, str] | None = None) -> str | None:
    return env_value(BLOB_BUCKET_ENV_VAR, env=env)


def storage_feature_enabled(env: Mapping[str, str] | None = None) -> bool:
    return resolve_storage_provider(env) is not None


def resolve_storage_provider(env: Mapping[str, str] | None = None) -> str | None:
    raw = (env_value(STORAGE_PROVIDER_ENV_VAR, env=env) or "").lower()
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
    return env_value(STORAGE_BUCKET_ENV_VAR, env=env)


def resolve_storage_endpoint_url(env: Mapping[str, str] | None = None) -> str | None:
    return env_value(STORAGE_ENDPOINT_URL_ENV_VAR, env=env)


def resolve_storage_region(env: Mapping[str, str] | None = None) -> str | None:
    return env_value(STORAGE_REGION_ENV_VAR, env=env)


def resolve_storage_access_key_id(env: Mapping[str, str] | None = None) -> str | None:
    return env_value(STORAGE_ACCESS_KEY_ID_ENV_VAR, env=env) or env_value(
        "AWS_ACCESS_KEY_ID", env=env
    )


def resolve_storage_secret_access_key(
    env: Mapping[str, str] | None = None,
) -> str | None:
    return env_value(STORAGE_SECRET_ACCESS_KEY_ENV_VAR, env=env) or env_value(
        "AWS_SECRET_ACCESS_KEY", env=env
    )


def resolve_mgmt_key_path(env: Mapping[str, str] | None = None) -> str | None:
    return env_value(MGMT_KEY_PATH_ENV_VAR, env=env)


def resolve_mgmt_public_key(env: Mapping[str, str] | None = None) -> str | None:
    return env_value(MGMT_PUBLIC_KEY_ENV_VAR, env=env)


def resolve_allowed_origins(env: Mapping[str, str] | None = None) -> list[str]:
    """Hosted-control CORS allowlist from a comma-separated env var."""
    raw = env_value(ALLOWED_ORIGINS_ENV_VAR, env=env) or ""
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


def resolve_ui_base_url(env: Mapping[str, str] | None = None) -> str:
    """Hosted UI base for the sign-in handoff, or "" when unset."""
    raw = (env_value(UI_BASE_URL_ENV_VAR, env=env) or "").rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(
            f"invalid {UI_BASE_URL_ENV_VAR}: {raw!r} "
            "(expected an http:// or https:// URL, path suffix allowed)",
            details={"url": raw},
        )
    return raw


def build_blob_store(
    *, default_root: Path, env: Mapping[str, str] | None = None
):
    """The submitted-byte BlobStore selected by configuration.

    A bucket name selects ``S3BlobStore`` (boto3 imported only on that branch,
    so local installs never need it); otherwise a ``LocalDirBlobStore`` rooted
    at the configured dir or ``default_root``. Same protocol + contract tests
    either way, so callers stay blob-implementation blind. Hosted/no-checkout
    control validates that a bucket is present at its composition root.
    """
    bucket = resolve_blob_bucket(env)
    if bucket:
        from ..object_storage.s3_blobs import S3BlobStore

        return S3BlobStore(bucket=bucket)
    from ..object_storage.blobs import LocalDirBlobStore

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
    from ..object_storage.s3_object_store import S3CompatibleObjectStore

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

    No URL selects the SQLite store at ``db_path``.
    A postgres:// URL selects the Postgres dialect (psycopg imported only on
    that branch, so local installs never need it). Any other scheme refuses
    to start rather than guessing a dialect.
    """
    url = resolve_db_url(env)
    if url is None:
        from ..kernel.state import StateStore

        return StateStore(db_path=db_path)
    if url.startswith(_POSTGRES_URL_PREFIXES):
        from ..kernel.state.dialects import PostgresStateStore

        return PostgresStateStore(dsn=url)
    raise ValidationError(
        f"unsupported {DB_URL_ENV_VAR} scheme: {url!r} "
        "(expected postgres:// or postgresql://, or unset for SQLite)",
        details={"db_url": url},
    )
