"""Lean FastAPI composition root for the Merv HTTP surface."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from .... import __version__
from ..admin_http import register_admin_routes
from ..data_plane_http import register_data_plane_routes
from ..feed_http import register_feed_routes
from ..http_policy import HttpSurfacePolicy
from ..mcp_http import register_mcp_routes
from . import artifacts, claims, events, experiments, litreview, meta, projects, reflections, reviews, sandboxes, storage, user_settings
from .context import ApiRouteContext
from .dependencies import HttpDependencies
from .gateway import (
    ProjectAuthorizer,
    RequestAuthenticator,
    ToolInvocationGateway,
    install_auth_routes,
    install_request_middleware,
)
from .sandbox_control import KEY_SANDBOX_CONTROL_TOOLS
from .middleware import (
    install_activity_middleware,
    install_cors,
    install_error_handlers,
)
def create_fastapi_app(
    app: HttpDependencies | None = None,
    *,
    allowed_origins: list[str] | None = None,
    cleanup: Any | None = None,
    tenant_counters: Any | None = None,
    surface_policy: HttpSurfacePolicy | None = None,
    auth: Any | None = None,
    ui_base_url: str = "",
    oauth_resource_uri: str = "",
) -> FastAPI:
    """Compose transport adapters around an already-built backend."""
    if app is None:
        raise ValueError("provide app")
    surface = surface_policy or HttpSurfacePolicy.for_surface(
        restrict_cors=False, hosted_control=False
    )
    api = app
    authorizer = ProjectAuthorizer(projects=api.projects)
    gateway = ToolInvocationGateway(
        tools=api.tools,
        reviews=api.reviews,
        sandboxes=api.sandboxes,
        surface=surface,
        projects=authorizer,
    )
    authenticator = RequestAuthenticator(surface=surface, verifier=auth)
    http = FastAPI(title="Merv API", version=__version__)

    install_request_middleware(http, authenticator=authenticator, authorizer=authorizer)
    install_activity_middleware(http, structured_logger=api.structured_log)
    # Registered last so CORS decorates middleware short-circuits as well.
    install_cors(http, allowed_origins=allowed_origins, surface=surface)
    install_error_handlers(http)
    install_auth_routes(http, verifier=auth, allowed_origins=allowed_origins,
                        ui_base_url=ui_base_url, owner_key_audience=oauth_resource_uri)

    ctx = ApiRouteContext(surface=surface, route_call_tool=gateway.call,
                          auth_meta=auth.meta() if auth is not None else None)
    routers = (
        meta.build_router(ctx, activity_log=api.activity, tool_calls=api.tool_calls),
        projects.build_router(
            ctx,
            projects=api.projects,
            dashboard=api.dashboard,
            workflow=api.workflow,
            timeline=api.timeline,
            sandboxes=api.sandboxes,
        ),
        claims.build_router(ctx),
        experiments.build_router(
            ctx,
            collection=api.experiment_collection,
            detail=api.experiment_detail,
            workflow=api.workflow,
            figure=api.experiment_figure,
            graphs=api.logic_graph,
            tracking=api.tracking_overview,
        ),
        reflections.build_router(graphs=api.logic_graph),
        litreview.build_router(literature=api.literature),
        artifacts.build_router(submissions=api.artifact_submissions),
        storage.build_router(storage=api.storage),
        reviews.build_router(ctx, review_delivery=api.reviews),
        sandboxes.build_router(ctx, sandboxes=api.sandboxes, cost_query=api.compute_cost),
        events.build_router(timeline=api.timeline),
        user_settings.build_router(user_settings=api.user_settings),
    )
    for router in routers:
        http.include_router(router)
    register_feed_routes(
        http,
        feed_api=api.feed,
        authorize_project=gateway.authorize_data_plane_project,
        activity=api.activity,
    )
    register_mcp_routes(
        http,
        list_tools=api.tools.list_tools,
        call_tool=gateway.call_mcp,
        # Data tools stay proxy-only over MCP except the key-sandbox surface an
        # mk_ key reaches over control (Phase C); the gateway gates who is served.
        allow_tool=lambda tool: tool.get("plane") != "data"
        or tool.get("name") in KEY_SANDBOX_CONTROL_TOOLS,
    )
    register_data_plane_routes(
        http,
        authorize_project=gateway.authorize_data_plane_project,
        feed=api.feed,
        sandboxes=api.sandboxes,
    )
    register_admin_routes(
        http,
        cleanup=cleanup,
        tenant_counters=tenant_counters or api.tenant_counters,
    )
    return http
