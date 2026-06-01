"""Exceptions for the job-runtime subsystem.

Defined inside the subpackage so job-runtime stays free of imports from the
surrounding research-plugin app. Callers translate these to app-level tool
errors at the boundary.
"""

from __future__ import annotations


class ExecutionBackendError(Exception):
    """Base error for execution backends."""


class BackendValidationError(ExecutionBackendError):
    """Caller-supplied job spec or backend hints are malformed."""


class BackendPermissionError(ExecutionBackendError):
    """Caller-supplied job spec or environment violates execution policy."""


class BackendUnavailableError(ExecutionBackendError):
    """The selected backend cannot be reached or initialized."""
