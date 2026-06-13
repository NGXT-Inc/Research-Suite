"""Cost governance: per-tenant quota schema + the admission enforcement seam.

Cloud plan Phase 7. Platform-owned provider credentials make spend control a
hard blocker, same class as auth (decision 3): the cloud holds the Lambda/Modal
keys, so it — not the user — pays for every VM, and a tenant with no ceiling
could run the bill unbounded.

Local-mode invariant: the implicit 'local' tenant has NO ``tenant_quotas`` row,
which reads as unlimited, so ``check_admission`` is a no-op and local behavior
is byte-identical. Enforcement bites only when a tenant has a quota row and a
ceiling on it is exceeded.

This is the schema + enforcement primitive. It is wired at the two procurement
choke points (the sandbox request admission path) but, like everything else in
Phase 7, it is dormant under the single local tenant; the live spend
kill-switch / GPU-hour accounting is Phase 9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..state.store import BaseStateStore, row_to_dict
from ..utils import PermissionDeniedError


@dataclass(frozen=True)
class AdmissionRequest:
    """The cost-relevant facts of a sandbox request, for quota admission.

    Decoupled from execution.SandboxRequest so the quota seam stays a pure
    record-layer concern (no provider types). ``price_usd_per_hour`` is the
    quoted price for the chosen instance (0/None when the provider has none,
    e.g. Modal), checked against the tenant's price ceiling.
    """

    tenant_id: str
    time_limit_seconds: int
    price_usd_per_hour: float | None = None


@dataclass(frozen=True)
class TenantQuota:
    """A tenant's ceilings. Every field None = unlimited for that dimension."""

    max_concurrent_sandboxes: int | None = None
    max_time_limit_seconds: int | None = None
    max_price_usd_per_hour: float | None = None
    gpu_hours_budget: float | None = None
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
                       max_price_usd_per_hour, gpu_hours_budget, blob_bytes_budget
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
            blob_bytes_budget=_int_or_none(data.get("blob_bytes_budget")),
        )

    def set_quota(self, *, tenant_id: str, **fields: Any) -> None:
        """Upsert a tenant's quota row (control-plane admin / tests)."""
        columns = (
            "max_concurrent_sandboxes",
            "max_time_limit_seconds",
            "max_price_usd_per_hour",
            "gpu_hours_budget",
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

    def check_admission(self, *, request: AdmissionRequest) -> None:
        """Admit a sandbox procurement, or raise PermissionDeniedError.

        No-op for any tenant with no quota row (unlimited) — which is exactly
        local mode's 'local' tenant. Enforced dimensions: concurrent sandboxes
        (counted from running rows scoped to the tenant), the per-request
        time_limit ceiling, and the instance-price ceiling. GPU-hour / USD /
        blob-byte budgets are recorded in the schema but their running-total
        accounting is Phase 9.
        """
        quota = self.get_quota(tenant_id=request.tenant_id)
        if quota is None:
            return
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


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)
