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
# the Postgres dialect — used only by tests and the future control profile;
# ResearchPluginApp stays SQLite-only until Phase 8 wires the control
# composition.
DB_URL_ENV_VAR = "RESEARCH_PLUGIN_DB_URL"

_POSTGRES_URL_PREFIXES = ("postgres://", "postgresql://")

# Modes the migration plan defines but later phases implement. Recognized so
# the error says "not yet implemented" instead of "unknown mode".
PLANNED_MODES = ("control", "daemon")


class Mode(str, Enum):
    LOCAL = "local"


def resolve_mode(env: Mapping[str, str] | None = None) -> Mode:
    """Resolve the process mode from the environment, failing fast."""
    source = env if env is not None else os.environ
    raw = (source.get(MODE_ENV_VAR) or "").strip().lower() or Mode.LOCAL.value
    if raw == Mode.LOCAL.value:
        return Mode.LOCAL
    if raw in PLANNED_MODES:
        raise ValidationError(
            f"RESEARCH_PLUGIN_MODE={raw!r} is not implemented yet; "
            "only 'local' is available (see docs/CLOUD_BACKEND_MIGRATION_PLAN.md)",
            details={"mode": raw},
        )
    raise ValidationError(
        f"unknown RESEARCH_PLUGIN_MODE: {raw!r} (expected 'local')",
        details={"mode": raw},
    )


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

    Resolved directly from the env value rather than through ``resolve_mode``
    so it answers truthfully for ``control`` even though ``resolve_mode`` still
    refuses to *start* a control process at runtime (the mode is wired but the
    composition lands in Phase 8). ``daemon`` authenticates upstream to the
    control plane, not its own callers, so it is auth-off locally.
    """
    source = env if env is not None else os.environ
    raw = (source.get(MODE_ENV_VAR) or "").strip().lower() or Mode.LOCAL.value
    return raw == "control"


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
