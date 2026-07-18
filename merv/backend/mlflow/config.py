"""MLflow-extension env configuration.

These env vars configure only the MLflow extension, so their resolution lives
inside the extension (module-boundary fix, phase 4a): the mlflow module must
not import the surface's ``backend.config``. Process-role config
(``RESEARCH_PLUGIN_REQUIRE_AGENT_MLFLOW`` enforcement, mode selection) stays in
``backend.config`` where the composition roots read it.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..env import env_value
from ..utils import ValidationError

MLFLOW_MODE_ENV_VAR = "MERV_MLFLOW_MODE"
MLFLOW_TRACKING_URI_ENV_VAR = "MERV_MLFLOW_TRACKING_URI"
MLFLOW_SERVER_URI_ENV_VAR = "MERV_MLFLOW_SERVER_URI"
MLFLOW_DASHBOARD_URL_ENV_VAR = "MERV_MLFLOW_DASHBOARD_URL"
# RapidReview API key handed to agents as MLFLOW_TRACKING_USERNAME/PASSWORD so
# runs anywhere can log through the Caddy-authenticated /mlflow route.
MLFLOW_AGENT_KEY_ENV_VAR = "MERV_MLFLOW_AGENT_KEY"
MLFLOW_AGENT_USERNAME = "rp-agent"


def resolve_mlflow_mode(env: Mapping[str, str] | None = None) -> str:
    """Centralized MLflow mode, or '' when MLflow is not configured yet."""
    raw = (env_value(MLFLOW_MODE_ENV_VAR, env=env) or "").lower()
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
    return (env_value(MLFLOW_TRACKING_URI_ENV_VAR, env=env) or "").rstrip("/")


def resolve_mlflow_server_uri(env: Mapping[str, str] | None = None) -> str:
    """Optional backend-internal MLflow URI for control-plane reads."""
    return (env_value(MLFLOW_SERVER_URI_ENV_VAR, env=env) or "").rstrip("/")


def resolve_mlflow_dashboard_url(env: Mapping[str, str] | None = None) -> str:
    """Optional human-facing MLflow dashboard URL; defaults to tracking URI."""
    return (env_value(MLFLOW_DASHBOARD_URL_ENV_VAR, env=env) or "").rstrip("/")


def resolve_mlflow_agent_key(env: Mapping[str, str] | None = None) -> str:
    """Optional agent credential for the authenticated hosted MLflow route."""
    return (env_value(MLFLOW_AGENT_KEY_ENV_VAR, env=env) or "")
