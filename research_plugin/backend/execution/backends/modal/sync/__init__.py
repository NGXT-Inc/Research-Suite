"""Local-repo ↔ Modal-volume synchronization for the modal execution backend.

All sync logic is contained in this package. The rest of the application sees
sync only through the ExecutionBackend protocol (submit/status/materialize_outputs)
and the shared activity log.
"""

from .baseline import BaselineStore
from .engine import SyncEngine
from .types import SyncError
from .lock import InterProcessSyncLock
from .poller import SyncPoller
from .types import ConflictRecord, FileFingerprint, SyncPlan, SyncResult


__all__ = [
    "BaselineStore",
    "ConflictRecord",
    "FileFingerprint",
    "InterProcessSyncLock",
    "SyncEngine",
    "SyncError",
    "SyncPlan",
    "SyncPoller",
    "SyncResult",
]
