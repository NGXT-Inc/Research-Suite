"""Dependency-free feature availability for the local proxy."""

from __future__ import annotations

from merv.shared.client_config import dual_env_value
from merv.shared.errors import ValidationError


_STORAGE_PROVIDER_ENV_VAR = "MERV_STORAGE_PROVIDER"


def storage_feature_enabled() -> bool:
    raw = (dual_env_value(_STORAGE_PROVIDER_ENV_VAR) or "").lower()
    if not raw:
        return False
    if raw != "s3":
        raise ValidationError(
            f"unknown {_STORAGE_PROVIDER_ENV_VAR}: {raw!r} "
            "(expected 's3', or unset to disable storage)",
            details={"provider": raw},
        )
    return True
