"""Context shared by resource route modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

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
