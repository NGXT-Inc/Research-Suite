"""Cross-cutting helpers for the Research Plugin backend.

Holds the small, dependency-free utilities every layer needs:

  - Domain error hierarchy (``ResearchPluginError`` and subclasses) used by
    services and surfaced through the MCP / HTTP boundary.
  - ``new_id(prefix=...)`` for opaque, prefixed entity ids.
  - ``now_iso()`` for the canonical UTC ISO-8601 timestamp string.

Keeping these in one module means every service can ``from ..utils import …``
once instead of three times.
"""

from __future__ import annotations

from datetime import UTC, datetime
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


# ---------------------------------------------------------------------------
# Identifier + clock helpers
# ---------------------------------------------------------------------------


def new_id(*, prefix: str) -> str:
    """Return an opaque id of the form ``"<prefix>_<12-hex-chars>"``."""
    return f"{prefix}_{uuid4().hex[:12]}"


def now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string (``…Z``)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
