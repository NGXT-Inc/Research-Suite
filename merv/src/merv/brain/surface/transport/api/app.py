"""Lean FastAPI composition root for the Merv HTTP surface."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from .... import __version__
from ..admin_http import register_admin_routes
from ..data_plane_http import register_data_plane_routes
from ..http_policy import HttpSurfacePolicy
from ..mcp_http import register_mcp_routes
from . import (
    claims,
    events,
    experiments,
    feed,
    meta,
    projects,
    reflections,
    resources,
    reviews,
    sandboxes,
    storage,
)
from .context import ApiRouteContext
from .gateway import (
    ProjectAuthorizer,
    RequestAuthenticator,
    ToolInvocationGateway,
    install_activity_middleware,
    install_auth_routes,
    install_cors,
    install_error_handlers,
    install_request_middleware,
)
from .views import ResearchHttpApi


ROUTE_BUILDERS = (
    meta.build_router,
    projects.build_router,
    claims.build_router,
    experiments.build_router,
    reflections.build_router,
    resources.build_router,
    storage.build_router,
    reviews.build_router,
    sandboxes.build_router,
    events.build_router,
    feed.build_router,
)


def create_fastapi_app(
    app: Any | None = None,
    *,
    allowed_origins: list[str] | None = None,
    cleanup: Any | None = None,
    tenant_counters: Any | None = None,
    surface_policy: HttpSurfacePolicy | None = None,
    auth: Any | None = None,
    ui_base_url: str = "",
) -> FastAPI:
    """Compose transport adapters around an already-built backend."""
    if app is None:
        raise ValueError("provide app")
    surface = surface_policy or HttpSurfacePolicy.for_surface(
        restrict_cors=False,
        hosted_control=False,
    )
    api = ResearchHttpApi(app=app)
    authorizer = ProjectAuthorizer(member_lookup=api.app.projects.is_member)
    gateway = ToolInvocationGateway(
        backend=api.app,
        surface=surface,
        projects=authorizer,
    )
    authenticator = RequestAuthenticator(surface=surface, verifier=auth)
    http = FastAPI(title="Merv API", version=__version__)

    install_request_middleware(
        http,
        authenticator=authenticator,
        authorizer=authorizer,
    )
    install_activity_middleware(http, structured_logger=api.app.structured_logger)
    # Registered last so CORS decorates middleware short-circuits as well.
    install_cors(http, allowed_origins=allowed_origins, surface=surface)
    install_error_handlers(http)
    install_auth_routes(
        http,
        verifier=auth,
        allowed_origins=allowed_origins,
        ui_base_url=ui_base_url,
    )

    ctx = ApiRouteContext(
        api=api,
        surface=surface,
        route_call_tool=gateway.call,
        auth_meta=auth.meta() if auth is not None else None,
    )
    for build_router in ROUTE_BUILDERS:
        http.include_router(build_router(ctx))
    register_mcp_routes(
        http,
        list_tools=api.app.list_tools,
        call_tool=gateway.call_mcp,
        allow_tool=lambda tool: tool.get("plane") != "data",
    )
    register_data_plane_routes(
        http,
        app_for_project=gateway.app_for_data_plane_project,
    )
    register_admin_routes(
        http,
        cleanup=cleanup,
        tenant_counters=tenant_counters or api.app.tenant_counters_query,
    )
    return http
