"""Compatibility shim for project-reflection thresholds."""

from __future__ import annotations

from ..domain.reflection_policy import (
    REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD,
    REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD,
)

__all__ = [
    "REFLECTION_BLOCK_NEW_TERMINAL_THRESHOLD",
    "REFLECTION_NUDGE_NEW_TERMINAL_THRESHOLD",
]
