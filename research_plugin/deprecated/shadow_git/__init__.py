"""Shadow git subsystem: backend-owned content store for resource snapshots.

Public surface kept stable for the rest of the app:
    ShadowGitStore, SnapshotUnavailableError, DEFAULT_MAX_SNAPSHOT_BYTES.
"""

from ._policy import DEFAULT_MAX_SNAPSHOT_BYTES, is_enabled
from .errors import (
    ShadowGitCommitError,
    ShadowGitConfigError,
    ShadowGitError,
    ShadowGitPathError,
    ShadowGitUnavailableError,
    SnapshotUnavailableError,
)
from .store import ShadowGitStore

__all__ = [
    "DEFAULT_MAX_SNAPSHOT_BYTES",
    "ShadowGitCommitError",
    "ShadowGitConfigError",
    "ShadowGitError",
    "ShadowGitPathError",
    "ShadowGitStore",
    "ShadowGitUnavailableError",
    "SnapshotUnavailableError",
    "is_enabled",
]
