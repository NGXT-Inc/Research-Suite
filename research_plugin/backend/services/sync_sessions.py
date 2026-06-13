"""Sync sessions, leases, and the control-plane view of sync targets.

Phase 4 of docs/CLOUD_BACKEND_MIGRATION_PLAN.md (fixed decision 8): every
sandbox byte movement — the initial push, periodic syncs, the final pull — is
authorized by an exclusive per-experiment **lease** and described by a
**sync session** the data-plane worker executes. The lease lives in the record
store (cloud-held in split mode) because the control plane is the only thing
every client can see: TTL + takeover is the whole multi-client coordination
story, never peer-to-peer. The session carries the SSH endpoint, the remote
directory contract, and a ``direction_policy`` that closes the rsync
``--delete`` footgun: the worker refuses to move bytes under a policy its
flags do not implement.

In local mode one process holds one implicit lease per experiment (acquire by
the same holder renews), so the agent surface is unchanged. In split mode
(Phase 8) these same payloads cross HTTP unmodified, and
``InProcessControlPlaneView.sync_targets`` is exactly the call that becomes
the daemon's poll.

Deadlines and expiries minted here are cloud-authoritative (plan §3.2): the
data plane treats them as opaque strings and never compares them against its
own clock — one clock in-process today.
"""

from __future__ import annotations

import posixpath
from datetime import UTC, datetime
from typing import Any

from ..execution.sync_dirs import (
    ARTIFACTS_TO_KEEP_DIRNAME,
    DEFAULT_DATA_DIR,
    remote_experiment_dir,
    remote_root_of,
    remote_sessions_dir,
)
from ..execution.transfer_spec import TRANSFER_CONTRACT_VERSION
from ..state.store import StateStore, row_to_dict
from ..utils import PermissionDeniedError, ResearchPluginError, new_id, now_iso
from .sandbox_registry import SandboxRegistry
from .sandbox_support import iso_after, parse_iso


SYNC_SESSION_SCHEMA_VERSION = 1
# TRANSFER_CONTRACT_VERSION pins the shared transfer rules (rsync excludes +
# size caps) into every session; it lives in execution/transfer_spec.py
# (plan Phase 5), the one module feeding both rsync and the parachute tar,
# and is re-exported here for session consumers.
# How long a sync lease lives between renewals. Comfortably above the
# auto-sync poll interval (~5s), short enough that a dead client's experiment
# is takeover-able quickly.
DEFAULT_LEASE_TTL_SECONDS = 120
# Budget handed to a final_pull task before the control plane gives up on the
# data plane. Unenforced in-process (the local worker is always reachable);
# the expiry parachute (plan Phase 5, decision 5) fires when the pull fails
# or a split-mode daemon misses the deadline.
DEFAULT_FINAL_PULL_DEADLINE_SECONDS = 120

# Per-subtree authority (plan §3.1 / fixed decision 8). These name what the
# rsync flags already implement: the experiment dir is mirrored with the
# remote as the authority while a sandbox lives (pull --delete), and
# artifacts_to_keep rides its own append-only-shaped 5 GB pass.
EXPERIMENT_DIR_POLICY = "remote_authoritative_for_results"
ARTIFACTS_TO_KEEP_POLICY = "remote_append_only"
DIRECTION_POLICY: dict[str, str] = {
    "experiment_dir": EXPERIMENT_DIR_POLICY,
    "artifacts_to_keep": ARTIFACTS_TO_KEEP_POLICY,
}


def build_sync_session(
    *,
    experiment_id: str,
    sandbox_id: str,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    experiment_dir: str,
    data_dir: str = "",
    lease: dict[str, Any],
) -> dict[str, Any]:
    """The byte-movement contract handed to the data plane (plan Phase 4).

    Remote dirs derive from the experiment dir via the sync_dirs contract;
    ``lease`` is session-shaped (``{id, holder_client_id, ttl_seconds,
    expires_at}``) as returned by ``LeaseService.acquire``.
    """
    experiment_dir = experiment_dir.rstrip("/")
    return {
        "schema_version": SYNC_SESSION_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "sandbox_id": sandbox_id,
        "ssh": {
            "host": ssh_host,
            "port": int(ssh_port or 0),
            "user": ssh_user or "root",
        },
        "remote": {
            "experiment_dir": experiment_dir,
            "data_dir": data_dir or DEFAULT_DATA_DIR,
            "sessions_dir": remote_sessions_dir(
                experiment_id=experiment_id, root=remote_root_of(experiment_dir)
            ),
            "artifacts_to_keep": posixpath.join(
                experiment_dir, ARTIFACTS_TO_KEEP_DIRNAME
            ),
        },
        "lease": dict(lease),
        "direction_policy": dict(DIRECTION_POLICY),
        "transfer_contract_version": TRANSFER_CONTRACT_VERSION,
    }


class LeaseService:
    """Exclusive per-experiment sync leases: acquire/renew/release/takeover.

    The acquire rules (plan Phase 4): a free experiment grants a fresh lease;
    re-acquire by the current holder renews in place (the local-mode implicit
    holder); an expired lease is takeover-able by any client, which mints a
    new lease id so the previous holder's completion reports turn stale; a
    live foreign lease is refused with the holder named, so the second client
    can see why it isn't syncing.
    """

    def __init__(self, *, store: StateStore) -> None:
        self.store = store

    def acquire(
        self,
        *,
        experiment_id: str,
        holder_client_id: str,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> dict[str, Any]:
        now = now_iso()
        ttl_seconds = int(ttl_seconds)
        with self.store.transaction() as conn:
            current = row_to_dict(
                row=conn.execute(
                    "SELECT * FROM sync_leases WHERE experiment_id = ?",
                    (experiment_id,),
                ).fetchone()
            )
            held_by_us = current is not None and (
                current["holder_client_id"] == holder_client_id
            )
            if current is not None and not held_by_us and not _expired(current):
                raise PermissionDeniedError(
                    f"experiment {experiment_id} is being synced by client "
                    f"{current['holder_client_id']} (lease {current['lease_id']} "
                    f"expires {current['expires_at']}); wait for that lease to "
                    "expire or release the sandbox from that client",
                    details={
                        "experiment_id": experiment_id,
                        "holder_client_id": current["holder_client_id"],
                        "expires_at": current["expires_at"],
                    },
                )
            # Same holder keeps its lease id (a renewal); a fresh grant or a
            # takeover mints a new one, invalidating the old holder's reports.
            lease_id = current["lease_id"] if held_by_us else new_id(prefix="lease")
            expires_at = iso_after(seconds=ttl_seconds)
            conn.execute(
                """
                INSERT INTO sync_leases
                  (experiment_id, lease_id, holder_client_id, ttl_seconds,
                   expires_at, renewed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                  lease_id = excluded.lease_id,
                  holder_client_id = excluded.holder_client_id,
                  ttl_seconds = excluded.ttl_seconds,
                  expires_at = excluded.expires_at,
                  renewed_at = excluded.renewed_at
                """,
                (experiment_id, lease_id, holder_client_id, ttl_seconds, expires_at, now),
            )
        return {
            "id": lease_id,
            "holder_client_id": holder_client_id,
            "ttl_seconds": ttl_seconds,
            "expires_at": expires_at,
        }

    def renew(
        self,
        *,
        experiment_id: str,
        lease_id: str,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        now = now_iso()
        with self.store.transaction() as conn:
            current = row_to_dict(
                row=conn.execute(
                    "SELECT * FROM sync_leases WHERE experiment_id = ?",
                    (experiment_id,),
                ).fetchone()
            )
            if current is None or current["lease_id"] != lease_id:
                raise PermissionDeniedError(
                    self._stale_message(
                        experiment_id=experiment_id, lease_id=lease_id, current=current
                    )
                )
            ttl = int(ttl_seconds if ttl_seconds is not None else current["ttl_seconds"])
            expires_at = iso_after(seconds=ttl)
            conn.execute(
                """
                UPDATE sync_leases
                SET ttl_seconds = ?, expires_at = ?, renewed_at = ?
                WHERE experiment_id = ?
                """,
                (ttl, expires_at, now, experiment_id),
            )
        return {
            "id": lease_id,
            "holder_client_id": str(current["holder_client_id"]),
            "ttl_seconds": ttl,
            "expires_at": expires_at,
        }

    def release(self, *, experiment_id: str, lease_id: str) -> None:
        """Drop the lease. Idempotent for an absent row; a foreign id is refused."""
        with self.store.transaction() as conn:
            current = row_to_dict(
                row=conn.execute(
                    "SELECT * FROM sync_leases WHERE experiment_id = ?",
                    (experiment_id,),
                ).fetchone()
            )
            if current is None:
                return
            if current["lease_id"] != lease_id:
                raise PermissionDeniedError(
                    self._stale_message(
                        experiment_id=experiment_id, lease_id=lease_id, current=current
                    )
                )
            conn.execute(
                "DELETE FROM sync_leases WHERE experiment_id = ?", (experiment_id,)
            )

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Delete every lease whose ``expires_at`` is at/before ``now``.

        The cloud lease-expiry sweep (cloud plan Phase 9): an abandoned lease
        (a dead client that never released) is normally taken over by the next
        ``acquire`` (a live foreign lease only blocks while unexpired), so this
        is housekeeping — it stops the table accumulating dead rows and makes a
        free experiment observably free. Clock-injectable for tests; returns the
        count removed. Idempotent.
        """
        now_dt = now or datetime.now(tz=UTC)
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT experiment_id, expires_at FROM sync_leases"
            ).fetchall()
            stale = [
                str(row["experiment_id"])
                for row in rows
                if _expired_at(row["expires_at"], now=now_dt)
            ]
            for experiment_id in stale:
                conn.execute(
                    "DELETE FROM sync_leases WHERE experiment_id = ?",
                    (experiment_id,),
                )
        return len(stale)

    def holder(self, *, experiment_id: str) -> dict[str, Any] | None:
        """The current lease row (holder + expiry for the agent views), or None."""
        conn = self.store.connect()
        try:
            return row_to_dict(
                row=conn.execute(
                    "SELECT * FROM sync_leases WHERE experiment_id = ?",
                    (experiment_id,),
                ).fetchone()
            )
        finally:
            conn.close()

    def validate_completion(self, *, experiment_id: str, lease_id: str) -> None:
        """Lease-check a sync completion report (``sandbox_report_sync``, §3.1).

        A stale or foreign lease id means another client took the experiment
        over mid-sync; its bytes may be on disk but its report is rejected so
        the record stream never credits a superseded holder. A lease that
        merely expired without takeover (same id) still validates — the
        worker was just slow.
        """
        current = self.holder(experiment_id=experiment_id)
        if current is None or current["lease_id"] != lease_id:
            raise PermissionDeniedError(
                self._stale_message(
                    experiment_id=experiment_id, lease_id=lease_id, current=current
                )
            )

    @staticmethod
    def _stale_message(
        *, experiment_id: str, lease_id: str, current: dict[str, Any] | None
    ) -> str:
        if current is None:
            return (
                f"sync lease {lease_id} for experiment {experiment_id} no longer "
                "exists; run sandbox.sync again to acquire a fresh lease"
            )
        return (
            f"sync lease {lease_id} for experiment {experiment_id} is stale — the "
            f"current lease is {current['lease_id']} held by client "
            f"{current['holder_client_id']} (expires {current['expires_at']}); "
            "run sandbox.sync again to acquire a fresh lease"
        )


class SyncSessionService:
    """Issues lease-backed sync sessions for one data-plane client.

    The control-plane half of every byte movement: acquire (or renew) the
    experiment's lease for this client, then describe the transfer as a
    session the worker executes. ``report_completion`` is the lease-checked
    record half.
    """

    def __init__(self, *, leases: LeaseService, client_id: str) -> None:
        self.leases = leases
        self.client_id = client_id

    def grant(
        self,
        *,
        experiment_id: str,
        sandbox_id: str,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        experiment_dir: str,
        data_dir: str = "",
    ) -> dict[str, Any]:
        lease = self.leases.acquire(
            experiment_id=experiment_id, holder_client_id=self.client_id
        )
        return build_sync_session(
            experiment_id=experiment_id,
            sandbox_id=sandbox_id,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            experiment_dir=experiment_dir,
            data_dir=data_dir,
            lease=lease,
        )

    def grant_for_row(self, *, row: dict[str, Any], name: str = "") -> dict[str, Any]:
        """Grant a session from a sandboxes row's provider-portable facts."""
        experiment_id = str(row.get("experiment_id") or "")
        return self.grant(
            experiment_id=experiment_id,
            sandbox_id=str(row.get("sandbox_id") or ""),
            ssh_host=str(row.get("ssh_host") or ""),
            ssh_port=int(row.get("ssh_port") or 0),
            ssh_user=str(row.get("ssh_user") or "root"),
            experiment_dir=str(
                row.get("sync_dir")
                or row.get("workdir")
                or remote_experiment_dir(experiment_id=experiment_id, name=name)
            ),
            data_dir=str(row.get("sandbox_data_dir") or row.get("unsynced_dir") or ""),
        )

    def report_completion(self, *, experiment_id: str, lease_id: str) -> None:
        self.leases.validate_completion(
            experiment_id=experiment_id, lease_id=lease_id
        )


class InProcessControlPlaneView:
    """The auto-sync poller's window onto the control plane (plan Phase 4).

    "My running sandboxes, with a sync lease granted for each" — implemented
    in-process by the registry + lease service today; in split mode (Phase 8)
    this exact call becomes the daemon's HTTP poll. A row whose lease another
    client holds is simply absent from the targets (``sandbox.get`` shows
    that holder and its expiry), so two clients can never double-sync one
    experiment.
    """

    def __init__(
        self, *, registry: SandboxRegistry, sessions: SyncSessionService
    ) -> None:
        self.registry = registry
        self.sessions = sessions

    def sync_targets(self) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        for row in self.registry.list_running_rows():
            try:
                session = self.sessions.grant_for_row(row=row)
            except ResearchPluginError:
                continue  # leased to another client — not ours to sync
            targets.append({"row": row, "session": session})
        return targets


def _expired(lease: dict[str, Any]) -> bool:
    expires = parse_iso(lease.get("expires_at"))
    if expires is None:
        return True
    return datetime.now(tz=UTC) >= expires


def _expired_at(expires_at: Any, *, now: datetime) -> bool:
    expires = parse_iso(expires_at)
    if expires is None:
        return True
    return now >= expires
