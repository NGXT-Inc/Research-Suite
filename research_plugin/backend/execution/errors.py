"""Compatibility exports for sandbox backend errors."""

from __future__ import annotations

from ..sandbox_backend import (
    BackendPermissionError,
    BackendUnavailableError,
    BackendValidationError,
    ExecutionBackendError,
)


__all__ = [
    "BackendPermissionError",
    "BackendUnavailableError",
    "BackendValidationError",
    "ExecutionBackendError",
]
