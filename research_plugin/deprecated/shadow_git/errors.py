"""Exceptions for the shadow-git subsystem.

Defined inside the subpackage so shadow-git stays free of imports from the
surrounding research-plugin app.
"""

from __future__ import annotations


class ShadowGitError(Exception):
    """Base error for the shadow-git subsystem."""


class ShadowGitConfigError(ShadowGitError):
    """Invalid configuration (env vars, sizes, etc.)."""


class ShadowGitPathError(ShadowGitError):
    """A resource path is not safe to store in shadow git."""


class ShadowGitCommitError(ShadowGitError):
    """A git command failed while writing a snapshot."""


class ShadowGitUnavailableError(ShadowGitError):
    """Git is not installed or the shadow store cannot be opened."""


class SnapshotUnavailableError(ShadowGitError):
    """A previously recorded snapshot can no longer be read."""
