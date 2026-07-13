"""Sandbox quota admission and spend accounting.

Quota rows can cap concurrency, lifetime, price, GPU-hours, USD, and blob bytes.
Global or per-tenant kill switches refuse new provisioning. Spend is rebuilt
from the sandbox-generation ledger, including open generations billed through
``now``. A scope with no quota row or kill switch is unlimited; local mode uses
the implicit ``local`` tenant.
"""

from __future__ import annotations

from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterator

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

    def get_quota(
        self, *, tenant_id: str, conn: Any | None = None
    ) -> TenantQuota | None:
        """The tenant's quota row as a TenantQuota, or None = unlimited."""
        context = nullcontext(conn) if conn is not None else closing(self.store.connect())
        with context as db:
            row = db.execute(
                """
                SELECT max_concurrent_sandboxes, max_time_limit_seconds,
                       max_price_usd_per_hour, gpu_hours_budget, usd_budget,
                       blob_bytes_budget
                FROM tenant_quotas WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
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

    def running_sandbox_count(
        self, *, tenant_id: str, conn: Any | None = None
    ) -> int:
        """How many sandboxes the tenant has running OR provisioning.

        Provisioning rows must count: a GPU boot takes minutes, so counting
        only 'running' lets a burst of requests sail past the concurrency cap
        before the first VM ever reaches running. Tenancy is reached through
        the project: sandboxes carry project_id and projects carry tenant_id.
        """
        context = nullcontext(conn) if conn is not None else closing(self.store.connect())
        with context as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS n
                FROM sandboxes s
                JOIN projects p ON p.id = s.project_id
                WHERE p.tenant_id = ? AND s.status IN ('provisioning', 'running')
                """,
                (tenant_id,),
            ).fetchone()
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

    def kill_switch_tripped(
        self, *, scope: str, conn: Any | None = None
    ) -> dict[str, Any] | None:
        """The tripped kill-switch row for ``scope`` (reason + when), or None."""
        context = nullcontext(conn) if conn is not None else closing(self.store.connect())
        with context as db:
            row = db.execute(
                "SELECT reason, tripped_at FROM spend_kill_switches "
                "WHERE scope = ? AND tripped = 1",
                (scope,),
            ).fetchone()
        if row is None:
            return None
        data = row_to_dict(row=row) or {}
        return {"reason": data.get("reason") or "", "tripped_at": data.get("tripped_at")}

    # ---------- running-total spend accounting (cloud plan Phase 9) ----------

    def tenant_spend(
        self,
        *,
        tenant_id: str,
        now: datetime | None = None,
        conn: Any | None = None,
    ) -> dict[str, float]:
        """Reconstruct a tenant's running spend from the generation ledger.

        Sum over generations of ``price_usd_per_hour × runtime_hours``; an open
        generation (``ended_at IS NULL``) bills to ``now``. GPU-hours multiply
        each generation's runtime by its persisted accelerator count.
        Clock-injectable for tests.
        """
        now_dt = now or datetime.now(tz=UTC)
        context = nullcontext(conn) if conn is not None else closing(self.store.connect())
        with context as db:
            rows = db.execute(
                "SELECT gpu_count, price_usd_per_hour, started_at, ended_at "
                "FROM sandbox_generations WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        usd = 0.0
        gpu_hours = 0.0
        for row in rows:
            data = row_to_dict(row=row) or {}
            started = parse_iso(data.get("started_at"))
            if started is None:
                continue
            ended = parse_iso(data.get("ended_at")) or now_dt
            hours = max(0.0, (ended - started).total_seconds() / 3600.0)
            gpu_hours += hours * max(0, int(data.get("gpu_count") or 0))
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
        with self.store.transaction() as conn:
            self._check_admission(conn=conn, request=request)

    def reserve_provisioning(
        self,
        *,
        request: AdmissionRequest,
        reservation: Callable[[Any], None],
    ) -> None:
        """Serialize admission with the provisioning row that consumes it."""
        with self.store.transaction() as conn:
            self._check_admission(conn=conn, request=request)
            reservation(conn)

    def _check_admission(self, *, conn: Any, request: AdmissionRequest) -> None:
        # Kill-switches apply to every tenant, quota row or not — a tripped
        # global breaker halts ALL new provisioning, including 'local' if an
        # operator ever trips it (it never is in local mode: no row exists).
        self._check_kill_switch(conn=conn, scope=GLOBAL_SCOPE, label="platform")
        self._check_kill_switch(
            conn=conn, scope=request.tenant_id, label="tenant"
        )
        if request.sandbox_uid and conn.execute(
            "SELECT 1 FROM sandboxes s JOIN projects p ON p.id = s.project_id "
            "WHERE s.sandbox_uid = ? AND p.tenant_id = ? "
            "AND s.status IN ('provisioning', 'running')",
            (request.sandbox_uid, request.tenant_id),
        ).fetchone() is not None:
            return  # idempotent retry of an already-reserved sandbox
        quota = self.get_quota(conn=conn, tenant_id=request.tenant_id)
        if quota is None:
            return
        if (
            quota.max_concurrent_sandboxes is not None
            and self.running_sandbox_count(conn=conn, tenant_id=request.tenant_id)
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
        if quota.max_price_usd_per_hour is not None:
            if request.price_usd_per_hour is None:
                raise PermissionDeniedError(
                    "requested instance price could not be resolved",
                    details={"quota": "max_price_usd_per_hour", "unresolved": "price"},
                )
            if request.price_usd_per_hour > quota.max_price_usd_per_hour:
                raise PermissionDeniedError(
                    "requested instance price exceeds tenant ceiling "
                    f"(${quota.max_price_usd_per_hour}/hr)",
                    details={
                        "limit": quota.max_price_usd_per_hour,
                        "requested": request.price_usd_per_hour,
                        "quota": "max_price_usd_per_hour",
                    },
                )
        self._check_budget(
            conn=conn,
            tenant_id=request.tenant_id,
            quota=quota,
            price_usd_per_hour=request.price_usd_per_hour,
            gpu_count=request.gpu_count,
            reserved_seconds=request.time_limit_seconds,
        )

    def check_lifetime_extension(
        self,
        *,
        tenant_id: str,
        total_time_limit_seconds: int,
        price_usd_per_hour: float | None = None,
        gpu_count: int | None = None,
        sandbox_uid: str = "",
        remaining_time_limit_seconds: int = 0,
        reservation: Callable[[Any], None] | None = None,
    ) -> None:
        """Admit extra lifetime for an already-running sandbox.

        Concurrent-count is intentionally skipped: the sandbox already exists.
        Kill switches, budgets, per-sandbox lifetime, and price ceilings still
        apply.
        """
        with self.store.transaction() as conn:
            self._check_lifetime_extension(
                conn=conn,
                tenant_id=tenant_id,
                total_time_limit_seconds=total_time_limit_seconds,
                price_usd_per_hour=price_usd_per_hour,
                gpu_count=gpu_count,
                sandbox_uid=sandbox_uid,
                remaining_time_limit_seconds=remaining_time_limit_seconds,
            )
            if reservation is not None:
                reservation(conn)

    def _check_lifetime_extension(
        self,
        *,
        conn: Any,
        tenant_id: str,
        total_time_limit_seconds: int,
        price_usd_per_hour: float | None,
        gpu_count: int | None,
        sandbox_uid: str,
        remaining_time_limit_seconds: int,
    ) -> None:
        self._check_kill_switch(conn=conn, scope=GLOBAL_SCOPE, label="platform")
        self._check_kill_switch(conn=conn, scope=tenant_id, label="tenant")
        quota = self.get_quota(conn=conn, tenant_id=tenant_id)
        if quota is None:
            return
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
        if quota.max_price_usd_per_hour is not None:
            if price_usd_per_hour is None:
                raise PermissionDeniedError(
                    "running instance price could not be resolved",
                    details={"quota": "max_price_usd_per_hour", "unresolved": "price"},
                )
            if price_usd_per_hour > quota.max_price_usd_per_hour:
                raise PermissionDeniedError(
                    "running instance price exceeds tenant ceiling "
                    f"(${quota.max_price_usd_per_hour}/hr)",
                    details={
                        "limit": quota.max_price_usd_per_hour,
                        "requested": price_usd_per_hour,
                        "quota": "max_price_usd_per_hour",
                    },
                )
        self._check_budget(
            conn=conn,
            tenant_id=tenant_id,
            quota=quota,
            price_usd_per_hour=price_usd_per_hour,
            gpu_count=gpu_count,
            reserved_seconds=remaining_time_limit_seconds,
            exclude_sandbox_uid=sandbox_uid,
        )

    def _check_kill_switch(self, *, conn: Any, scope: str, label: str) -> None:
        tripped = self.kill_switch_tripped(conn=conn, scope=scope)
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

    def _check_budget(
        self,
        *,
        conn: Any,
        tenant_id: str,
        quota: "TenantQuota",
        price_usd_per_hour: float | None,
        gpu_count: int | None,
        reserved_seconds: int,
        exclude_sandbox_uid: str = "",
    ) -> None:
        """Reserve accrued spend plus every active/requested future commitment."""
        if quota.gpu_hours_budget is None and quota.usd_budget is None:
            return
        spend = self.tenant_spend(
            conn=conn, tenant_id=tenant_id, now=datetime.now(tz=UTC)
        )
        commitments = self._active_commitments(
            conn=conn,
            tenant_id=tenant_id,
            exclude_sandbox_uid=exclude_sandbox_uid,
        )
        hours = max(0, int(reserved_seconds)) / 3600.0
        if quota.gpu_hours_budget is not None:
            if gpu_count is None or commitments["gpu_unresolved"] or commitments[
                "duration_unresolved"
            ]:
                raise PermissionDeniedError(
                    "GPU count could not be resolved for budget reservation",
                    details={
                        "quota": "gpu_hours_budget",
                        "unresolved": (
                            "duration"
                            if commitments["duration_unresolved"]
                            else "gpu_count"
                        ),
                    },
                )
            projected_gpu = spend["gpu_hours"] + commitments["gpu_hours"] + hours * gpu_count
            if projected_gpu > quota.gpu_hours_budget:
                raise PermissionDeniedError(
                    "tenant GPU-hour budget would be exceeded "
                    f"({projected_gpu:.2f}/{quota.gpu_hours_budget} GPU-hours)",
                    details={
                        "limit": quota.gpu_hours_budget,
                        "projected": projected_gpu,
                        "quota": "gpu_hours_budget",
                    },
                )
        if quota.usd_budget is not None:
            if price_usd_per_hour is None or commitments["price_unresolved"] or commitments[
                "duration_unresolved"
            ]:
                raise PermissionDeniedError(
                    "instance price could not be resolved for budget reservation",
                    details={
                        "quota": "usd_budget",
                        "unresolved": (
                            "duration"
                            if commitments["duration_unresolved"]
                            else "price"
                        ),
                    },
                )
            projected_usd = spend["usd"] + commitments["usd"] + hours * price_usd_per_hour
            if projected_usd <= quota.usd_budget:
                return
            raise PermissionDeniedError(
                "tenant USD budget would be exceeded "
                f"(${projected_usd:.2f}/${quota.usd_budget})",
                details={
                    "limit": quota.usd_budget,
                    "projected": projected_usd,
                    "quota": "usd_budget",
                },
            )

    def _active_commitments(
        self, *, conn: Any, tenant_id: str, exclude_sandbox_uid: str = ""
    ) -> dict[str, float | bool]:
        rows = conn.execute(
            "SELECT s.sandbox_uid, s.status, s.price_usd_per_hour, s.price_known, s.gpu_count, "
            "s.time_limit, s.expires_at FROM sandboxes s "
            "JOIN projects p ON p.id = s.project_id WHERE p.tenant_id = ? "
            "AND s.status IN ('provisioning', 'running') AND s.sandbox_uid != ?",
            (tenant_id, exclude_sandbox_uid),
        ).fetchall()
        now = datetime.now(tz=UTC)
        out: dict[str, float | bool] = {
            "usd": 0.0,
            "gpu_hours": 0.0,
            "price_unresolved": False,
            "gpu_unresolved": False,
            "duration_unresolved": False,
        }
        for row in rows:
            data = row_to_dict(row=row) or {}
            if data.get("status") == "running":
                expires = parse_iso(data.get("expires_at"))
                if expires is None:
                    out["duration_unresolved"] = True
                    continue
                seconds = max(0.0, (expires - now).total_seconds())
            else:
                seconds = int(data.get("time_limit") or 0)
                if seconds <= 0:
                    out["duration_unresolved"] = True
                    continue
            hours = seconds / 3600.0
            if hours <= 0:
                continue
            if not int(data.get("price_known") or 0):
                out["price_unresolved"] = True
            else:
                out["usd"] = float(out["usd"]) + hours * float(
                    data.get("price_usd_per_hour") or 0.0
                )
            count = int(data.get("gpu_count") if data.get("gpu_count") is not None else -1)
            if count < 0:
                out["gpu_unresolved"] = True
            else:
                out["gpu_hours"] = float(out["gpu_hours"]) + hours * count
        return out


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
