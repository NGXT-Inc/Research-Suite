"""Small environment-variable coercion helpers.

Every helper resolves config names dual-spelled: ``MERV_X`` is the primary
spelling and ``RESEARCH_PLUGIN_X`` the legacy fallback. Either spelling may be
passed in; non-empty values win and empty strings count as unset for
precedence, matching the ``or``-chain semantics the resolvers always had. When
the legacy spelling is the effective source from the real process environment,
one deprecation warning per variable per process names the new spelling.
Names outside the two prefixes resolve unchanged.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping


ENV_PREFIX = "MERV_"
LEGACY_ENV_PREFIX = "RESEARCH_PLUGIN_"

FALSE_VALUES = {"0", "false", "no", "off"}

LOGGER = logging.getLogger(__name__)
_warned_legacy_names: set[str] = set()


def env_name_pair(name: str) -> tuple[str, str]:
    """The (primary, legacy) spellings of a config var, given either one."""
    if name.startswith(ENV_PREFIX):
        return name, LEGACY_ENV_PREFIX + name[len(ENV_PREFIX):]
    if name.startswith(LEGACY_ENV_PREFIX):
        return ENV_PREFIX + name[len(LEGACY_ENV_PREFIX):], name
    return name, name


def merv_env_name(name: str) -> str:
    """Canonical ``MERV_*`` spelling of ``name`` for user-facing messages."""
    return env_name_pair(name)[0]


def env_raw(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Dual-read raw value: stripped, ``""`` preserved, None when unset."""
    primary, legacy = env_name_pair(name)
    source = env if env is not None else os.environ
    raw_primary = source.get(primary)
    primary_value = None if raw_primary is None else str(raw_primary).strip()
    if primary_value:
        return primary_value
    raw_legacy = source.get(legacy) if legacy != primary else None
    legacy_value = None if raw_legacy is None else str(raw_legacy).strip()
    if legacy_value:
        # Only nag about the real process environment: explicit mappings are
        # internal plumbing (tests, composition) rather than operator config.
        if env is None and primary not in _warned_legacy_names:
            _warned_legacy_names.add(primary)
            LOGGER.warning(
                "%s is deprecated; set %s instead (the legacy value was used)",
                legacy,
                primary,
            )
        return legacy_value
    # Neither non-empty: preserve set-but-blank ("") over unset (None).
    return primary_value if primary_value is not None else legacy_value


def env_value(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Dual-read value with empty-as-unset: a non-empty string or None."""
    return env_raw(name, env=env) or None


def _reset_env_deprecation_warnings() -> None:
    """Test hook: forget which legacy names already warned this process."""
    _warned_legacy_names.clear()


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
    return env_raw(name, env=env)
