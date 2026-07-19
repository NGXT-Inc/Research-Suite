"""Cross-cutting helpers for the Merv backend.

Holds the small, dependency-free utilities every layer needs:

  - Domain error hierarchy (``ResearchPluginError`` and subclasses) used by
    services and surfaced through the MCP / HTTP boundary.
  - ``new_id(prefix=...)`` for opaque, prefixed entity ids.
  - ``now_iso()`` / ``format_iso()`` / ``parse_iso()`` for consistent ISO-8601
    timestamp handling.

Keeping these in one module means every service can ``from ..utils import …``
once instead of three times.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class ResearchPluginError(Exception):
    """Base class for domain and tool errors."""

    error_code = "research_plugin_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(ResearchPluginError):
    error_code = "not_found"


class PermissionDeniedError(ResearchPluginError):
    error_code = "permission_denied"


class ValidationError(ResearchPluginError):
    error_code = "validation_error"


class WorkflowError(ResearchPluginError):
    error_code = "workflow_error"


class ContentUnavailableError(ResearchPluginError):
    """A file's bytes are not reachable from this plane (cloud plan Phase 9).

    Raised when content lives only on an offline/absent data-plane daemon (or is
    metadata-only in the cloud, fixed decision 6). Distinct from NotFoundError so
    the UI can render an explicit "content unavailable in this mode" degraded
    state instead of treating it as a missing record.
    """

    error_code = "content_unavailable"


class DataPlaneRequiredError(ResearchPluginError):
    """The requested mutation must be performed by the local data plane."""

    error_code = "data_plane_required"


# ---------------------------------------------------------------------------
# Identifier + clock helpers
# ---------------------------------------------------------------------------


def new_id(*, prefix: str) -> str:
    """Return an opaque id of the form ``"<prefix>_<12-hex-chars>"``."""
    return f"{prefix}_{uuid4().hex[:12]}"


def now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string (``…Z``)."""
    return format_iso(datetime.now(UTC))


def iso_after(*, seconds: int) -> str:
    """Return the UTC instant ``seconds`` from now as an ISO-8601 string."""
    return format_iso(datetime.now(UTC) + timedelta(seconds=seconds))


def format_iso(value: datetime) -> str:
    """Return an ISO-8601 UTC timestamp string with second precision."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, normalizing naive values to UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def safe_experiment_dirname(experiment_id: str) -> str:
    """Filesystem-safe directory name for an experiment.

    Kernel-floor path primitive shared by the research-core folder layout
    (research_core/domain/paths.py) and the sandbox module's remote workdir naming
    (sandbox/sandbox_paths.py) without a cross-module edge.
    """
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in experiment_id) or "experiment"
