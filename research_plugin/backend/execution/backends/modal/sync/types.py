"""Sync data types: fingerprints, plans, results, and the subsystem error."""

from __future__ import annotations

from dataclasses import dataclass, field


class SyncError(Exception):
    """Base error for the modal sync subsystem."""


@dataclass(frozen=True)
class FileFingerprint:
    """Identity of a file at a point in time on one side (local or remote)."""

    path: str
    mtime_ns: int
    size_bytes: int


@dataclass(frozen=True)
class ConflictRecord:
    """A path that changed on both sides since the last baseline."""

    path: str
    local: FileFingerprint | None
    remote: FileFingerprint | None


@dataclass(frozen=True)
class SyncPlan:
    """The set of operations a sync pass intends to perform."""

    push: tuple[FileFingerprint, ...] = ()
    pull: tuple[FileFingerprint, ...] = ()
    delete_remote: tuple[str, ...] = ()
    delete_local: tuple[str, ...] = ()
    converged: tuple[FileFingerprint, ...] = ()  # both sides agreed; only baseline updates
    conflicts: tuple[ConflictRecord, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.push
            or self.pull
            or self.delete_remote
            or self.delete_local
            or self.converged
            or self.conflicts
        )


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a single sync pass for one project.

    Sync is always bidirectional — local and remote are mirrored both ways.
    """

    project_id: str
    pushed: int = 0
    pulled: int = 0
    deleted_remote: int = 0
    deleted_local: int = 0
    conflicts: int = 0
    duration_ms: int = 0
    skipped_conflicts: tuple[str, ...] = field(default_factory=tuple)
    # True iff the caller requested skip_if_busy AND both queue slots
    # (running + queued) were already full, so no work was performed.
    skipped_busy: bool = False
    # True iff the caller arrived while both queue slots were full and
    # waited for the already-queued sync to complete. The counts above
    # are the queued sync's actual counts (this caller's request "became"
    # that one).
    coalesced: bool = False
