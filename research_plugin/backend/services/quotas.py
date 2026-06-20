"""Cost governance: per-tenant quota schema + the admission enforcement seam.

Cloud plan Phase 7. Platform-owned provider credentials make spend control a
hard blocker, same class as auth (decision 3): the cloud holds the Lambda/Modal
keys, so it — not the user — pays for every VM, and a tenant with no ceiling
could run the bill unbounded.

Local-mode invariant: the implicit 'local' tenant has NO ``tenant_quotas`` row,
which reads as unlimited, so ``check_admission`` is a no-op and local behavior
is byte-identical. Enforcement bites only when a tenant has a quota row and a
ceiling on it is exceeded.

Phase 9 makes enforcement LIVE: a per-tenant and global spend kill-switch
(an operator circuit-breaker that refuses new provisioning when tripped) and
running-total USD/GPU-hour accounting reconstructed from the
``sandbox_generations`` ledger (sum over generations of price × runtime; an
open generation — ``ended_at IS NULL`` — bills to ``now``). ``check_admission``
now consults the kill-switches and the budget. The 'local' tenant still has no
quota row and no kill-switch, so all of this is dormant and local mode stays
byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..domain.quota_contract import AdmissionRequest
from ..state.store import BaseStateStore, row_to_dict
from ..utils import PermissionDeniedError, now_iso

# Scope key for the platform-wide kill-switch row (vs a per-tenant scope, which
# is the tenant id). Tenant ids are opaque strings; '__global__' cannot collide
# with one minted by ``new_id`` (those carry a ``tnt_``-style prefix).
GLOBAL_SCOPE = "__global__"


@dataclass(frozen=True)
class TenantQuota:
    """A tenant's ceilings. Every field None = unlimited for that dimension."""

    max_concurrent_sandboxes: int | None = None
    max_time_limit_seconds: int | None = None
    max_price_usd_per_hour: float | None = None
    gpu_hours_budget: float | None = None
    usd_budget: float | None = None
    blob_bytes_budget: int | None = None


class QuotaService:
    """Reads tenant quotas and admits or denies procurement against them."""

    def __init__(self, *, store: BaseStateStore) -> None:
        self.store = store

    def get_quota(self, *, tenant_id: str) -> TenantQuota | None:
        """The tenant's quota row as a TenantQuota, or None = unlimited."""
        conn = self.store.connect()
        try:
            row = conn.execute(
                """
                SELECT max_concurrent_sandboxes, max_time_limit_seconds,
                       max_price_usd_per_hour, gpu_hours_budget, usd_budget,
                       blob_bytes_budget
                FROM tenant_quotas WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        data = row_to_dict(row=row) or {}
        return TenantQuota(
            max_concurrent_sandboxes=_int_or_none(data.get("max_concurrent_sandboxes")),
            max_time_limit_seconds=_int_or_none(data.get("max_time_limit_seconds")),
            max_price_usd_per_hour=_float_or_none(data.get("max_price_usd_per_hour")),
            gpu_hours_budget=_float_or_none(data.get("gpu_hours_budget")),
            usd_budget=_float_or_none(data.get("usd_budget")),
            blob_bytes_budget=_int_or_none(data.get("blob_bytes_budget")),
        )

    def set_quota(self, *, tenant_id: str, **fields: Any) -> None:
        """Upsert a tenant's quota row (control-plane admin / tests)."""
        columns = (
            "max_concurrent_sandboxes",
            "max_time_limit_seconds",
            "max_price_usd_per_hour",
            "gpu_hours_budget",
            "usd_budget",
            "blob_bytes_budget",
        )
        values = {col: fields.get(col) for col in columns}
        with self.store.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM tenant_quotas WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    f"INSERT INTO tenant_quotas (tenant_id, {', '.join(columns)}) "
                    f"VALUES (?, {', '.join('?' for _ in columns)})",
                    (tenant_id, *(values[col] for col in columns)),
                )
            else:
                assignments = ", ".join(f"{col} = ?" for col in columns)
                conn.execute(
                    f"UPDATE tenant_quotas SET {assignments} WHERE tenant_id = ?",
                    (*(values[col] for col in columns), tenant_id),
                )

    def running_sandbox_count(self, *, tenant_id: str) -> int:
        """How many sandboxes the tenant currently has running.

        Tenancy is reached through the project: sandboxes carry project_id and
        projects carry tenant_id.
        """
        conn = self.store.connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM sandboxes s
                JOIN projects p ON p.id = s.project_id
                WHERE p.tenant_id = ? AND s.status = 'running'
                """,
                (tenant_id,),
            ).fetchone()
        finally:
            conn.close()
        return int(row["n"]) if row is not None else 0

    # ---------- spend kill-switch (cloud plan Phase 9, risk 13) ----------

    def set_kill_switch(
        self, *, scope: str, tripped: bool, reason: str = ""
    ) -> None:
        """Trip or arm a spend kill-switch (operator / runbook action).

        ``scope`` is a tenant id, or ``GLOBAL_SCOPE`` for the platform-wide
        breaker. Upserts so re-tripping just refreshes the reason/timestamp.
        """
        with self.store.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM spend_kill_switches WHERE scope = ?", (scope,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO spend_kill_switches "
                    "(scope, tripped, reason, tripped_at) VALUES (?, ?, ?, ?)",
                    (scope, 1 if tripped else 0, reason, now_iso() if tripped else None),
                )
            else:
                conn.execute(
                    "UPDATE spend_kill_switches "
                    "SET tripped = ?, reason = ?, tripped_at = ? WHERE scope = ?",
                    (1 if tripped else 0, reason, now_iso() if tripped else None, scope),
                )

    def kill_switch_tripped(self, *, scope: str) -> dict[str, Any] | None:
        """The tripped kill-switch row for ``scope`` (reason + when), or None."""
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT reason, tripped_at FROM spend_kill_switches "
                "WHERE scope = ? AND tripped = 1",
                (scope,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        data = row_to_dict(row=row) or {}
        return {"reason": data.get("reason") or "", "tripped_at": data.get("tripped_at")}

    # ---------- running-total spend accounting (cloud plan Phase 9) ----------

    def tenant_spend(
        self, *, tenant_id: str, now: datetime | None = None
    ) -> dict[str, float]:
        """Reconstruct a tenant's running spend from the generation ledger.

        Sum over generations of ``price_usd_per_hour × runtime_hours``; an open
        generation (``ended_at IS NULL``) bills to ``now``. GPU-hours here are
        billed wall-clock generation-hours (one GPU job = its runtime); the
        ledger does not yet carry a GPU count, so this is the conservative
        single-accelerator reading. Clock-injectable for tests.
        """
        now_dt = now or datetime.now(tz=UTC)
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT price_usd_per_hour, started_at, ended_at "
                "FROM sandbox_generations WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        finally:
            conn.close()
        usd = 0.0
        gpu_hours = 0.0
        for row in rows:
            data = row_to_dict(row=row) or {}
            started = _parse_iso(data.get("started_at"))
            if started is None:
                continue
            ended = _parse_iso(data.get("ended_at")) or now_dt
            hours = max(0.0, (ended - started).total_seconds() / 3600.0)
            gpu_hours += hours
            usd += hours * float(data.get("price_usd_per_hour") or 0.0)
        return {"usd": usd, "gpu_hours": gpu_hours}

    def check_admission(self, *, request: AdmissionRequest) -> None:
        """Admit a sandbox procurement, or raise PermissionDeniedError.

        Order of checks (cheapest/loudest first): the global then per-tenant
        spend kill-switch, then — for tenants with a quota row — the
        running-total USD/GPU-hour budgets, then the per-request concurrent /
        time_limit / instance-price ceilings.

        No-op for any tenant with no quota row AND no kill-switch (unlimited) —
        which is exactly local mode's 'local' tenant — so local mode is
        byte-identical.
        """
        # Kill-switches apply to every tenant, quota row or not — a tripped
        # global breaker halts ALL new provisioning, including 'local' if an
        # operator ever trips it (it never is in local mode: no row exists).
        self._check_kill_switch(scope=GLOBAL_SCOPE, label="platform")
        self._check_kill_switch(scope=request.tenant_id, label="tenant")
        quota = self.get_quota(tenant_id=request.tenant_id)
        if quota is None:
            return
        self._check_budget(tenant_id=request.tenant_id, quota=quota)
        if (
            quota.max_concurrent_sandboxes is not None
            and self.running_sandbox_count(tenant_id=request.tenant_id)
            >= quota.max_concurrent_sandboxes
        ):
            raise PermissionDeniedError(
                "tenant sandbox quota reached: "
                f"{quota.max_concurrent_sandboxes} concurrent sandboxes",
                details={
                    "limit": quota.max_concurrent_sandboxes,
                    "quota": "max_concurrent_sandboxes",
                },
            )
        if (
            quota.max_time_limit_seconds is not None
            and request.time_limit_seconds > quota.max_time_limit_seconds
        ):
            raise PermissionDeniedError(
                "requested time_limit exceeds tenant ceiling "
                f"({quota.max_time_limit_seconds}s)",
                details={
                    "limit": quota.max_time_limit_seconds,
                    "requested": request.time_limit_seconds,
                    "quota": "max_time_limit_seconds",
                },
            )
        if (
            quota.max_price_usd_per_hour is not None
            and request.price_usd_per_hour is not None
            and request.price_usd_per_hour > quota.max_price_usd_per_hour
        ):
            raise PermissionDeniedError(
                "requested instance price exceeds tenant ceiling "
                f"(${quota.max_price_usd_per_hour}/hr)",
                details={
                    "limit": quota.max_price_usd_per_hour,
                    "requested": request.price_usd_per_hour,
                    "quota": "max_price_usd_per_hour",
                },
            )

    def _check_kill_switch(self, *, scope: str, label: str) -> None:
        tripped = self.kill_switch_tripped(scope=scope)
        if tripped is None:
            return
        reason = tripped.get("reason") or "spend halt"
        raise PermissionDeniedError(
            f"new sandbox provisioning is halted by the {label} spend "
            f"kill-switch: {reason}",
            details={
                "kill_switch": label,
                "scope": scope,
                "reason": reason,
                "tripped_at": tripped.get("tripped_at"),
            },
        )

    def _check_budget(self, *, tenant_id: str, quota: "TenantQuota") -> None:
        """Refuse a new provision when the tenant's running total is over budget.

        Budgets are running totals reconstructed from the ledger, so this denies
        once the tenant has ALREADY spent up to its ceiling (the in-flight
        generation's own future cost is not pre-charged — the next admission
        catches it, and a generation's cost is bounded by its time_limit).
        """
        if quota.gpu_hours_budget is None and quota.usd_budget is None:
            return
        spend = self.tenant_spend(tenant_id=tenant_id)
        if (
            quota.gpu_hours_budget is not None
            and spend["gpu_hours"] >= quota.gpu_hours_budget
        ):
            raise PermissionDeniedError(
                "tenant GPU-hour budget exhausted "
                f"({spend['gpu_hours']:.2f}/{quota.gpu_hours_budget} GPU-hours)",
                details={
                    "limit": quota.gpu_hours_budget,
                    "spent": spend["gpu_hours"],
                    "quota": "gpu_hours_budget",
                },
            )
        if quota.usd_budget is not None and spend["usd"] >= quota.usd_budget:
            raise PermissionDeniedError(
                "tenant USD spend budget exhausted "
                f"(${spend['usd']:.2f}/${quota.usd_budget})",
                details={
                    "limit": quota.usd_budget,
                    "spent": spend["usd"],
                    "quota": "usd_budget",
                },
            )


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)
