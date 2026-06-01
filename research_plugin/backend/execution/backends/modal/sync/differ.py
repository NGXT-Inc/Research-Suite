"""Three-way diff: current local vs current remote vs last-synced baseline."""

from __future__ import annotations

from .types import ConflictRecord, FileFingerprint, SyncPlan


def three_way_diff(
    *,
    local: dict[str, FileFingerprint],
    remote: dict[str, FileFingerprint],
    baseline: dict[str, tuple[FileFingerprint | None, FileFingerprint | None]],
    conflict_paths: set[str] | None = None,
) -> SyncPlan:
    """Compute the sync plan from three snapshots.

    Decision rule per path (L=local, R=remote, LB/RB=baseline; None=absent):
      - L==LB and R==RB → no change, skip
      - L!=LB and R==RB → push (or delete remote if L is None)
      - L==LB and R!=RB → pull (or delete local if R is None)
      - both changed     → conflict (strict halt)

    Paths already in conflict_paths are skipped entirely until resolved.
    """
    conflict_paths = conflict_paths or set()

    push: list[FileFingerprint] = []
    pull: list[FileFingerprint] = []
    delete_remote: list[str] = []
    delete_local: list[str] = []
    converged: list[FileFingerprint] = []
    conflicts: list[ConflictRecord] = []

    all_paths = set(local) | set(remote) | set(baseline)
    for path in sorted(all_paths):
        if path in conflict_paths:
            continue

        L = local.get(path)
        R = remote.get(path)
        LB, RB = baseline.get(path, (None, None))

        local_changed = L != LB
        remote_changed = R != RB

        if not local_changed and not remote_changed:
            continue

        if local_changed and not remote_changed:
            if L is None:
                delete_remote.append(path)
            else:
                push.append(L)
            continue

        if remote_changed and not local_changed:
            if R is None:
                delete_local.append(path)
            else:
                pull.append(R)
            continue

        # Both sides changed.
        if L is None and R is None:
            # Both deleted, just drop the baseline row.
            converged.append(FileFingerprint(path=path, mtime_ns=0, size_bytes=0))
            continue

        conflicts.append(ConflictRecord(path=path, local=L, remote=R))

    return SyncPlan(
        push=tuple(push),
        pull=tuple(pull),
        delete_remote=tuple(delete_remote),
        delete_local=tuple(delete_local),
        converged=tuple(converged),
        conflicts=tuple(conflicts),
    )
