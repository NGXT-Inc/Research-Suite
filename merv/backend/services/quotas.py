"""Sandbox quota admission and spend accounting.

Quota rows can cap concurrency, lifetime, price, GPU-hours, USD, and blob bytes.
Global or per-tenant kill switches refuse new provisioning. Spend is rebuilt
from the sandbox-generation ledger, including open generations billed through
``now``. A scope with no quota row or kill switch is unlimited; the current
unauthenticated HTTP surface normally uses the implicit ``local`` tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

from ..ports.quota_admission import AdmissionRequest
from ..state.store import BaseStateStore, row_to_dict
from ..utils import PermissionDeniedError, now_iso, parse_iso

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
        """How many sandboxes the tenant has running OR provisioning.

        Provisioning rows must count: a GPU boot takes minutes, so counting
        only 'running' lets a burst of requests sail past the concurrency cap
        before the first VM ever reaches running. Tenancy is reached through
        the project: sandboxes carry project_id and projects carry tenant_id.
        """
        conn = self.store.connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM sandboxes s
                JOIN projects p ON p.id = s.project_id
                WHERE p.tenant_id = ? AND s.status IN ('provisioning', 'running')
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
            started = parse_iso(data.get("started_at"))
            if started is None:
                continue
            ended = parse_iso(data.get("ended_at")) or now_dt
            hours = max(0.0, (ended - started).total_seconds() / 3600.0)
            gpu_hours += hours
            usd += hours * float(data.get("price_usd_per_hour") or 0.0)
        return {"usd": usd, "gpu_hours": gpu_hours}

    def project_spend(
        self, *, project_id: str, now: datetime | None = None
    ) -> dict[str, Any]:
        """A project's compute spend from the generation ledger, grouped for the UI.

        Same billing rule as ``tenant_spend`` — price × wall-clock hours, an
        open generation bills to ``now`` — plus the groupings a spend panel
        needs: per experiment, per hardware SKU, and per UTC day (a generation
        spanning midnight is apportioned across the days it ran). Hours quoted
        at $0 (Modal, local) are summed separately so the UI can say "N hours
        unpriced" instead of passing off a partial total as the whole truth.
        """
        now_dt = now or datetime.now(tz=UTC)
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT experiment_id, instance_type, gpu, price_usd_per_hour, "
                "started_at, ended_at "
                "FROM sandbox_generations WHERE project_id = ? ORDER BY created_seq",
                (project_id,),
            ).fetchall()
        finally:
            conn.close()
        totals = {"usd": 0.0, "hours": 0.0, "unpriced_hours": 0.0}
        open_generations = 0
        burn_usd_per_hour = 0.0
        generations = 0
        by_experiment: dict[str, dict[str, Any]] = {}
        by_hardware: dict[tuple[str, str, float], dict[str, Any]] = {}
        daily: dict[str, dict[str, Any]] = {}
        for row in rows:
            data = row_to_dict(row=row) or {}
            started = parse_iso(data.get("started_at"))
            if started is None:
                continue
            ended = parse_iso(data.get("ended_at"))
            end = ended or now_dt
            hours = max(0.0, (end - started).total_seconds() / 3600.0)
            price = float(data.get("price_usd_per_hour") or 0.0)
            usd = hours * price
            generations += 1
            totals["usd"] += usd
            totals["hours"] += hours
            if price <= 0:
                totals["unpriced_hours"] += hours
            if ended is None:
                open_generations += 1
                burn_usd_per_hour += price
            exp_id = str(data.get("experiment_id") or "")
            exp = by_experiment.setdefault(
                exp_id,
                {"experiment_id": exp_id, "usd": 0.0, "hours": 0.0, "generations": 0},
            )
            exp["usd"] += usd
            exp["hours"] += hours
            exp["generations"] += 1
            hw_key = (
                str(data.get("instance_type") or ""),
                str(data.get("gpu") or ""),
                price,
            )
            hw = by_hardware.setdefault(
                hw_key,
                {
                    "instance_type": hw_key[0],
                    "gpu": hw_key[1],
                    "price_usd_per_hour": price,
                    "usd": 0.0,
                    "hours": 0.0,
                    "generations": 0,
                },
            )
            hw["usd"] += usd
            hw["hours"] += hours
            hw["generations"] += 1
            for day, day_hours in _hours_by_utc_day(started=started, ended=end):
                bucket = daily.setdefault(day, {"date": day, "usd": 0.0, "hours": 0.0})
                bucket["usd"] += day_hours * price
                bucket["hours"] += day_hours
        def by_spend(entry: dict[str, Any]) -> tuple[float, float]:
            return (-entry["usd"], -entry["hours"])

        return {
            "total_usd": totals["usd"],
            "total_hours": totals["hours"],
            "unpriced_hours": totals["unpriced_hours"],
            "generations": generations,
            "open_generations": open_generations,
            "burn_usd_per_hour": burn_usd_per_hour,
            "by_experiment": sorted(by_experiment.values(), key=by_spend),
            "by_hardware": sorted(by_hardware.values(), key=by_spend),
            "daily": sorted(daily.values(), key=lambda bucket: bucket["date"]),
        }

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

    def check_lifetime_extension(
        self,
        *,
        tenant_id: str,
        total_time_limit_seconds: int,
        price_usd_per_hour: float | None = None,
    ) -> None:
        """Admit extra lifetime for an already-running sandbox.

        Concurrent-count is intentionally skipped: the sandbox already exists.
        Kill switches, budgets, per-sandbox lifetime, and price ceilings still
        apply.
        """
        self._check_kill_switch(scope=GLOBAL_SCOPE, label="platform")
        self._check_kill_switch(scope=tenant_id, label="tenant")
        quota = self.get_quota(tenant_id=tenant_id)
        if quota is None:
            return
        self._check_budget(tenant_id=tenant_id, quota=quota)
        if (
            quota.max_time_limit_seconds is not None
            and total_time_limit_seconds > quota.max_time_limit_seconds
        ):
            raise PermissionDeniedError(
                "extended sandbox lifetime exceeds tenant ceiling "
                f"({quota.max_time_limit_seconds}s)",
                details={
                    "limit": quota.max_time_limit_seconds,
                    "requested": total_time_limit_seconds,
                    "quota": "max_time_limit_seconds",
                },
            )
        if (
            quota.max_price_usd_per_hour is not None
            and price_usd_per_hour is not None
            and price_usd_per_hour > quota.max_price_usd_per_hour
        ):
            raise PermissionDeniedError(
                "running instance price exceeds tenant ceiling "
                f"(${quota.max_price_usd_per_hour}/hr)",
                details={
                    "limit": quota.max_price_usd_per_hour,
                    "requested": price_usd_per_hour,
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


def _hours_by_utc_day(
    *, started: datetime, ended: datetime
) -> Iterator[tuple[str, float]]:
    """Yield (YYYY-MM-DD, hours) portions of [started, ended) per UTC day."""
    cursor = started.astimezone(UTC)
    ended = ended.astimezone(UTC)
    while cursor < ended:
        next_midnight = datetime(
            cursor.year, cursor.month, cursor.day, tzinfo=UTC
        ) + timedelta(days=1)
        chunk_end = min(ended, next_midnight)
        yield cursor.date().isoformat(), (chunk_end - cursor).total_seconds() / 3600.0
        cursor = chunk_end


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)
