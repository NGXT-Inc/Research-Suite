"""Central mode/config resolution for the backend.

The cloud split (docs/CLOUD_BACKEND_MIGRATION_PLAN.md) gives the backend three
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

from .utils import ValidationError

if TYPE_CHECKING:  # the store import stays lazy at runtime (see build_state_store)
    from .state import BaseStateStore


MODE_ENV_VAR = "RESEARCH_PLUGIN_MODE"

# Record-store selection (cloud plan Phase 6). Absent ⇒ the SQLite default
# (local mode, today's behavior, byte-identical). A postgres:// URL selects
# the Postgres dialect — used by tests and the control profile.
DB_URL_ENV_VAR = "RESEARCH_PLUGIN_DB_URL"

# Split-transport config (cloud plan Phase 8, §3.4). The daemon dials the cloud
# at CONTROL_URL with a bearer token read from CONTROL_TOKEN_FILE; the cloud
# never dials in. The blob bucket/dir selects the BlobStore impl per mode.
CONTROL_URL_ENV_VAR = "RESEARCH_PLUGIN_CONTROL_URL"
CONTROL_TOKEN_FILE_ENV_VAR = "RESEARCH_PLUGIN_CONTROL_TOKEN_FILE"
BLOB_DIR_ENV_VAR = "RESEARCH_PLUGIN_BLOB_DIR"
BLOB_BUCKET_ENV_VAR = "RESEARCH_PLUGIN_BLOB_BUCKET"

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


def resolve_auth_required(env: Mapping[str, str] | None = None) -> bool:
    """Whether the HTTP surface must authenticate every request (plan Phase 7).

    Derived from the mode, NOT from a separate switch, so auth-on and the
    control topology are the same decision:

    - ``local`` (default) ⇒ False: auth off, loopback bind enforced by
      http_server, single implicit 'local' tenant — today's behavior.
    - ``control`` ⇒ True: mandatory bearer auth on every route.
    - ``daemon`` ⇒ False: it authenticates UPSTREAM to the control plane, not
      its own (loopback) callers. The daemon's own loopback hardening (a local
      auth secret) is a separate Phase 8 concern, not this bearer gate.
    """
    return resolve_mode(env) is Mode.CONTROL


def resolve_control_url(env: Mapping[str, str] | None = None) -> str | None:
    """The cloud control-plane URL the daemon dials, or None (plan §3.4)."""
    source = env if env is not None else os.environ
    return (source.get(CONTROL_URL_ENV_VAR) or "").strip().rstrip("/") or None


def resolve_control_token(env: Mapping[str, str] | None = None) -> str | None:
    """Read the daemon's cloud bearer token from its 0600 token file (§3.4).

    The file is JSON ``{"token": "..."}`` (matching the credentials.json shape
    in the config matrix); a bare token line is also accepted. Never logged.
    Returns None when no token file is configured (local control-to-daemon).
    """
    source = env if env is not None else os.environ
    path = (source.get(CONTROL_TOKEN_FILE_ENV_VAR) or "").strip()
    if not path:
        return None
    try:
        import json

        raw = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw  # a bare token line
    if isinstance(parsed, dict):
        token = parsed.get("token")
        return str(token) if token else None
    return raw


def resolve_blob_dir(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(BLOB_DIR_ENV_VAR) or "").strip() or None


def resolve_blob_bucket(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return (source.get(BLOB_BUCKET_ENV_VAR) or "").strip() or None


def build_blob_store(
    *, default_root: Path, env: Mapping[str, str] | None = None
):
    """The BlobStore the configuration selects (cloud plan Phase 8).

    A bucket name selects ``S3BlobStore`` (boto3 imported only on that branch,
    so local installs never need it); otherwise a ``LocalDirBlobStore`` rooted
    at the configured dir or ``default_root``. Same protocol + contract tests
    either way, so callers stay blob-impl-blind. The control profile MUST set a
    bucket — its "presign" must be a real HTTPS PUT a sandbox VM can reach (a
    LocalDirBlobStore loopback token cannot, breaking the parachute).
    """
    bucket = resolve_blob_bucket(env)
    if bucket:
        from .state.s3_blobs import S3BlobStore

        return S3BlobStore(bucket=bucket)
    from .state.blobs import LocalDirBlobStore

    root = resolve_blob_dir(env)
    return LocalDirBlobStore(root=Path(root) if root else default_root)


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
