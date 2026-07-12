"""Machine client config and secret-file helpers shared by launchers."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path


CLIENT_CONFIG_ENV_VAR = "RESEARCH_PLUGIN_CLIENT_CONFIG"
CONTROL_URL_ENV_VAR = "RESEARCH_PLUGIN_CONTROL_URL"
# RapidReview API key (rr_sk_...) for the hosted brain; env beats the
# client.json "api_key" field, mirroring the control_url chain.
API_KEY_ENV_VAR = "RESEARCH_PLUGIN_API_KEY"
DAEMON_STATE_DIR_ENV_VAR = "RESEARCH_PLUGIN_DAEMON_STATE_DIR"
# Brain URL defaults: unconfigured machines dial the hosted brain; local
# deployments opt in via `merv-client configure` or the env var.
HOSTED_CONTROL_URL = "https://experiments.rapidreview.io"
LOCAL_BRAIN_URL = "http://127.0.0.1:8787"
DEFAULT_CLIENT_CONFIG_PATH = Path.home() / ".research_plugin" / "client.json"
DEFAULT_DAEMON_SECRET_PATH = Path.home() / ".research_plugin" / "daemon_secret"
DAEMON_SECRET_FILE_NAME = "daemon_secret"


def resolve_client_config_path(env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    raw = (source.get(CLIENT_CONFIG_ENV_VAR) or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_CLIENT_CONFIG_PATH


def read_client_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    path = resolve_client_config_path(env)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items() if value is not None}


def read_secret_file(path: str | Path | None, *, keys: tuple[str, ...] = ("token",)) -> str | None:
    if not path:
        return None
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(parsed, dict):
        for key in keys:
            value = parsed.get(key)
            if value:
                return str(value)
    return raw
