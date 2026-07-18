"""Machine client config and secret-file helpers shared by launchers.

Env-var names resolve dual-spelled here exactly as in ``backend.env``:
``MERV_X`` primary, ``RESEARCH_PLUGIN_X`` legacy fallback (non-empty wins;
empty counts as unset). The logic is duplicated tiny rather than imported —
this package must stay stdlib-only with no backend imports (the zero-install
stdio proxy ships it), and the proxy may only ever write warnings to stderr
because stdout carries the JSON-RPC stream.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path


ENV_PREFIX = "MERV_"
LEGACY_ENV_PREFIX = "RESEARCH_PLUGIN_"
_warned_legacy_names: set[str] = set()


def env_name_pair(name: str) -> tuple[str, str]:
    """The (primary, legacy) spellings of a config var, given either one."""
    if name.startswith(ENV_PREFIX):
        return name, LEGACY_ENV_PREFIX + name[len(ENV_PREFIX):]
    if name.startswith(LEGACY_ENV_PREFIX):
        return ENV_PREFIX + name[len(LEGACY_ENV_PREFIX):], name
    return name, name


def dual_env_value(
    name: str, env: Mapping[str, str] | None = None
) -> str | None:
    """Dual-read a config var: a non-empty stripped value or None.

    When the legacy spelling is the effective source from the real process
    environment, one stderr deprecation line per variable per process names
    the new spelling. stderr only: the proxy's stdout is the MCP stream.
    """
    primary, legacy = env_name_pair(name)
    source = env if env is not None else os.environ
    value = (source.get(primary) or "").strip()
    if value:
        return value
    legacy_value = (source.get(legacy) or "").strip() if legacy != primary else ""
    if legacy_value:
        if env is None and primary not in _warned_legacy_names:
            _warned_legacy_names.add(primary)
            print(
                f"[merv] {legacy} is deprecated; set {primary} instead "
                "(the legacy value was used)",
                file=sys.stderr,
            )
        return legacy_value
    return None


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
