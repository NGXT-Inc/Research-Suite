"""MLflow-extension env configuration.

These env vars configure only the MLflow extension, so their resolution lives
inside the extension (module-boundary fix, phase 4a): the mlflow module must
not import the surface's ``backend.config``. Process-role config
(``RESEARCH_PLUGIN_REQUIRE_AGENT_MLFLOW`` enforcement, mode selection) stays in
``backend.config`` where the composition roots read it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from ..utils import ValidationError

MLFLOW_MODE_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_MODE"
MLFLOW_TRACKING_URI_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_TRACKING_URI"
MLFLOW_SERVER_URI_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_SERVER_URI"
MLFLOW_DASHBOARD_URL_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_DASHBOARD_URL"
# RapidReview API key handed to agents as MLFLOW_TRACKING_USERNAME/PASSWORD so
# runs anywhere can log through the Caddy-authenticated /mlflow route.
MLFLOW_AGENT_KEY_ENV_VAR = "RESEARCH_PLUGIN_MLFLOW_AGENT_KEY"
MLFLOW_AGENT_USERNAME = "rp-agent"


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


def resolve_mlflow_agent_key(env: Mapping[str, str] | None = None) -> str:
    """Optional agent credential for the authenticated hosted MLflow route."""
    source = env if env is not None else os.environ
    return (source.get(MLFLOW_AGENT_KEY_ENV_VAR) or "").strip()
