"""Context shared by resource route modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Request

from ...identity import LOCAL_PRINCIPAL
from ..http_policy import HttpSurfacePolicy
from .views import ResearchHttpApi


@dataclass(frozen=True)
class ApiRouteContext:
    api: ResearchHttpApi
    surface: HttpSurfacePolicy
    route_call_tool: Callable[..., dict[str, Any]]
    # Public auth block for /api/meta (required flag + supabase url/anon key);
    # None on the local surface, which advertises no auth at all.
    auth_meta: dict[str, Any] | None = None

    def call_tool(
        self,
        request: Request,
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        project_scope: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a route-backed tool through the shared auth/policy gateway."""
        result = self.route_call_tool(
            name=name,
            arguments=arguments,
            project_scope=project_scope,
            activity_source="http",
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )
        return self.api._present(result)
