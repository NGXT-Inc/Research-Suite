"""Small environment-variable coercion helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping


FALSE_VALUES = {"0", "false", "no", "off"}


def env_bool(
    name: str, default: bool = False, *, env: Mapping[str, str] | None = None
) -> bool:
    value = _env_value(name=name, env=env)
    if value is None or value == "":
        return default
    return value.lower() not in FALSE_VALUES


def env_float(
    name: str,
    override: float | None,
    default: float,
    *,
    env: Mapping[str, str] | None = None,
    strict: bool = False,
) -> float:
    if override is not None:
        return float(override)
    value = _env_value(name=name, env=env)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except ValueError:
        if strict:
            raise
        return float(default)


def env_int(
    name: str,
    default: int,
    *,
    env: Mapping[str, str] | None = None,
    strict: bool = True,
) -> int:
    value = _env_value(name=name, env=env)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        if strict:
            raise
        return int(default)


def _env_value(*, name: str, env: Mapping[str, str] | None) -> str | None:
    value = (env if env is not None else os.environ).get(name)
    return None if value is None else str(value).strip()
