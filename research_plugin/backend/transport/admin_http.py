"""Control-admin HTTP routes kept out of the general UI route factory."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request

from ..observability import TenantCounters


def register_admin_routes(
    http: Any,
    *,
    store: Any | None,
    cleanup: Any | None,
    require_admin: Callable[[Request], Any],
    require_tenant_or_admin: Callable[[Request, str], Any],
) -> None:
    if cleanup is None:
        return

    @http.post("/api/admin/cleanup")
    def admin_cleanup(request: Request) -> dict[str, Any]:
        require_admin(request)
        return {"cleaned": cleanup.run_all().as_dict()}

    @http.get("/api/admin/tenants/{tenant_id}/counters")
    def admin_tenant_counters(tenant_id: str, request: Request) -> dict[str, Any]:
        require_tenant_or_admin(request, tenant_id)
        assert store is not None
        return TenantCounters(store=store).for_tenant(tenant_id=tenant_id)
