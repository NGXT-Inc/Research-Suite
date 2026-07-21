"""Control-admin HTTP routes kept out of the general UI route factory."""

from __future__ import annotations

from typing import Any


def register_admin_routes(
    http: Any,
    *,
    cleanup: Any | None,
    tenant_counters: Any | None,
) -> None:
    if cleanup is None:
        return

    @http.post("/api/admin/cleanup")
    def admin_cleanup() -> dict[str, Any]:
        return {"cleaned": cleanup.run_all().as_dict()}

    @http.get("/api/admin/tenants/{tenant_id}/counters")
    def admin_tenant_counters(tenant_id: str) -> dict[str, Any]:
        assert tenant_counters is not None
        return tenant_counters(tenant_id=tenant_id)
