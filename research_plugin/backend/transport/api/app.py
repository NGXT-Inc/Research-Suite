"""FastAPI app assembly for the Research Plugin HTTP surface."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from ... import __version__
from ...app import ResearchPluginApp
from ...daemon.project_router import ProjectRouter
from ...services.identity import LOCAL_PRINCIPAL
from ...state import monotonic_ms
from ...tools.contracts import DATA_PLANE_TOOL_NAMES, PROJECT_SCOPED_TOOL_NAMES, TOOL_CONTRACTS
from ...utils import (
    ContentUnavailableError,
    DataPlaneRequiredError,
    NotFoundError,
    ResearchPluginError,
    ValidationError,
)
from ...version import CLIENT_VERSION_HEADER, MIN_PROXY_VERSION, is_below_floor
from ..admin_http import register_admin_routes
from ..data_plane_http import register_data_plane_routes
from ..http_policy import (
    HOSTED_CONTROL_TOOL_POLICIES,
    HTTP_DATA_PLANE_FEATURE_TO_TOOL,
    HttpSurfacePolicy,
)
from ..mcp_http import register_mcp_routes
from .context import ApiRouteContext
from .shared import UI_CORS_EXPOSE_HEADERS, UI_CORS_HEADERS, is_local_origin
from .views import ResearchHttpApi
from . import claims, events, experiments, feed, meta, projects, reflections, resources, reviews, sandboxes, storage


def create_fastapi_app(
    app: ResearchPluginApp | None = None,
    *,
    router: ProjectRouter | None = None,
    allowed_origins: list[str] | None = None,
    cleanup: Any | None = None,
    surface_policy: HttpSurfacePolicy | None = None,
) -> FastAPI:
    # HTTP surface seam. Local mode exposes data-plane routes and accepts
    # repo_root context. Hosted control hides local data-plane tool calls and
    # relies on the local MCP proxy to resolve repo_root -> project_id.
    if (app is None) == (router is None):
        raise ValueError("provide exactly one of app or router")
    surface = surface_policy or HttpSurfacePolicy.for_surface(
        restrict_cors=False,
        hosted_control=False,
        expose_local_data_plane=True,
    )
    api = (
        ResearchHttpApi(app=app, expose_local_data_plane=surface.expose_local_data_plane)
        if app is not None
        else None
    )

    def api_for_project(project_id: str) -> ResearchHttpApi:
        if router is not None:
            return ResearchHttpApi(
                app=router.app_for_project(project_id),
                expose_local_data_plane=surface.expose_local_data_plane,
            )
        assert api is not None
        return api

    def default_api() -> ResearchHttpApi | None:
        if api is not None:
            return api
        assert router is not None
        project_app = router.any_app()
        return (
            ResearchHttpApi(app=project_app, expose_local_data_plane=surface.expose_local_data_plane)
            if project_app is not None
            else None
        )

    def route_call_tool(
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        activity_source: str = "http",
        principal: Any | None = None,
    ) -> dict[str, Any]:
        arguments = dict(arguments or {})
        context = dict(context or {})
        contract = TOOL_CONTRACTS.get(name)

        def validated_contract_input() -> Any:
            assert contract is not None
            try:
                return contract.input_model.model_validate(arguments)
            except PydanticValidationError as exc:
                raise ValidationError(
                    "invalid tool arguments",
                    details={"tool": name, "errors": exc.errors()},
                ) from exc

        if not surface.accept_repo_root_context and context.get("repo_root"):
            raise DataPlaneRequiredError(
                "repo_root context is local data-plane state; hosted control "
                "requires the local MCP proxy to resolve and send project_id",
                details={
                    "field": "context.repo_root",
                    "reason": "repo_root_hidden_from_cloud",
                },
            )
        if not surface.allow_data_plane_tool_calls and name in DATA_PLANE_TOOL_NAMES:
            raise DataPlaneRequiredError(
                f"{name} requires the local MCP proxy; hosted control mode "
                "cannot read local files, hold user SSH keys, or run rsync",
                details={
                    "tool": name,
                    "reason": "requires_local_data_plane",
                },
            )
        if router is not None:
            return router.call_tool(
                name=name,
                arguments=arguments,
                context=context,
                activity_source=activity_source,
            )
        assert api is not None
        policy = (
            HOSTED_CONTROL_TOOL_POLICIES.get(name)
            if surface.use_hosted_tool_policies
            else None
        )
        if policy is not None:
            call_kwargs: dict[str, Any] = {}
            if policy.telemetry_from_review_request:
                call_kwargs["telemetry_project_id"] = (
                    api.app.reviews.request_project_id(
                        review_request_id=arguments.get("review_request_id")
                    )
                )
            return api.app.call_tool(
                name=name,
                arguments=arguments,
                activity_source=activity_source,
                **call_kwargs,
            )
        if name in PROJECT_SCOPED_TOOL_NAMES and "project_id" not in arguments and (context or {}).get("repo_root"):
            projects_result = api.app.projects.list_projects()["projects"]
            if len(projects_result) != 1:
                raise ValidationError(
                    "project_id is required when the repo has multiple projects",
                    details={"projects": [project["id"] for project in projects_result]},
                )
            arguments["project_id"] = projects_result[0]["id"]
        if (
            surface.hosted_control
            and contract is not None
            and contract.hosted_control_sandbox_lookup
        ):
            request = validated_contract_input()
            result = api.app.sandboxes.get(
                experiment_id=request.experiment_id,
                project_id=request.project_id,
                tenant_id=None,
                # Address the specific sandbox by its durable id (decoupled
                # identity); omitted still targets the experiment's primary.
                sandbox_uid=request.sandbox_uid,
                include_data_plane_enrichment=False,
            )
            return result
        return api.app.call_tool(name=name, arguments=arguments, activity_source=activity_source)

    def require_data_plane_for_http(*, feature: str) -> None:
        tool = HTTP_DATA_PLANE_FEATURE_TO_TOOL[feature]
        if surface.allow_data_plane_http:
            return
        raise DataPlaneRequiredError(
            f"{tool} requires local-mode HTTP; hosted control mode "
            "serves this API as an observer/admin surface",
            details={
                "tool": tool,
                "reason": "requires_local_data_plane",
            },
        )

    http = FastAPI(title="Research Plugin API", version=__version__)

    @http.middleware("http")
    async def reject_foreign_origins(request: Request, call_next):
        # Loopback CSRF guard (local surface only): the HTTP server binds
        # loopback, but any web page the user visits can still fire cross-origin
        # requests at 127.0.0.1 — and the wide-open CORS above would even let
        # it read the responses. Browsers always attach Origin cross-origin;
        # curl/proxy traffic is origin-less and the local UI is a localhost
        # origin, so rejecting foreign Origins blocks drive-by pages without
        # touching any legitimate caller. Hosted control (even with open
        # CORS) is a deliberate operator surface and stays out of this guard.
        origin = request.headers.get("origin")
        if (
            not surface.restrict_cors
            and not surface.hosted_control
            and origin
            and not is_local_origin(origin)
        ):
            return JSONResponse(
                {
                    "detail": "cross-origin requests to the local HTTP server are not allowed",
                    "error_code": "forbidden_origin",
                },
                status_code=403,
            )
        return await call_next(request)

    @http.middleware("http")
    async def attach_principal(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        request.state.principal = LOCAL_PRINCIPAL
        request.state.authenticated = False
        # /health is liveness; /api/meta is the version handshake itself.
        if request.url.path in ("/health", "/api/meta"):
            return await call_next(request)
        # Version/compat floor (cloud plan Phase 9). A below-floor client is
        # rejected with an actionable upgrade error. A missing header is
        # tolerated for pre-Phase-9 clients.
        client_version = request.headers.get(CLIENT_VERSION_HEADER)
        if surface.hosted_control and client_version and is_below_floor(
            client_version=client_version, floor=MIN_PROXY_VERSION
        ):
            return JSONResponse(
                {
                    "detail": (
                        f"client version {client_version} is below the minimum "
                        f"supported {MIN_PROXY_VERSION}; upgrade the research-plugin "
                        "client (pip install -U research-plugin) and reconnect"
                    ),
                    "error_code": "client_too_old",
                    "min_version": MIN_PROXY_VERSION,
                    "client_version": client_version,
                },
                status_code=426,
            )
        return await call_next(request)

    @http.middleware("http")
    async def log_http_activity(request: Request, call_next):
        started = monotonic_ms()
        status = 500
        # Per-request id for the structured cloud log stream (cloud plan
        # Phase 9). Echoed back on the response so a client/log line can be
        # correlated. Cheap stdlib uuid; no new dependency.
        import uuid

        request_id = uuid.uuid4().hex[:16]
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-RP-Request-Id"] = request_id
            return response
        finally:
            duration_ms = monotonic_ms() - started
            if api is not None:
                # Intentionally only collect MCP tool-call events in the shared
                # activity log (HTTP request telemetry was disabled per request).
                # Structured cloud log line (control mode only; dormant locally).
                # tenant_id comes from the resolved principal when present.
                principal = getattr(request.state, "principal", None)
                api.app.structured_logger.log(
                    kind="http",
                    request_id=request_id,
                    tenant_id=getattr(principal, "tenant_id", "") or "",
                    path=str(request.url.path),
                    status=status,
                    duration_ms=duration_ms,
                    method=request.method,
                )

    # CORS is registered LAST so it becomes the OUTERMOST middleware, wrapping
    # the version gate and origin guard above. Starlette applies user middleware
    # outermost-first in reverse registration order, so it must be added last to
    # decorate EVERY response — including a middleware short-circuit like the
    # version-gate 426. Otherwise a cross-origin UI (e.g. the Vercel build) gets
    # a CORS-less 426 the browser reports as an opaque "Load failed" instead of
    # the actionable upgrade error. Local mode keeps the wide-open `*` policy
    # (loopback-only, backed by reject_foreign_origins); control mode uses an
    # explicit allowed-origins list.
    if surface.restrict_cors:
        http.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins or [],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=UI_CORS_HEADERS,
            expose_headers=UI_CORS_EXPOSE_HEADERS,
        )
    else:
        http.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            # The hosted UI sends Authorization; every UI request also stamps
            # X-RP-Client-Version. Include both so dev overrides pass preflight.
            allow_headers=UI_CORS_HEADERS,
            expose_headers=UI_CORS_EXPOSE_HEADERS,
        )

    @http.exception_handler(ResearchPluginError)
    async def research_error_handler(_request: Request, exc: ResearchPluginError) -> JSONResponse:
        # 404 for missing records AND for content-unavailable (the bytes live on
        # a local data plane / are metadata-only): the error_code lets the UI
        # render an explicit degraded state rather than a generic error.
        status = 404 if isinstance(exc, (NotFoundError, ContentUnavailableError)) else 400
        return JSONResponse({"detail": exc.message, "error_code": exc.error_code, **exc.details}, status_code=status)

    @http.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "invalid HTTP request", "errors": exc.errors()}, status_code=400)

    def app_for_data_plane_project(request: Request, project_id: str) -> ResearchPluginApp:
        return api_for_project(project_id).app

    ctx = ApiRouteContext(
        api=api,
        project_router=router,
        surface=surface,
        cleanup=cleanup,
        api_for_project=api_for_project,
        default_api=default_api,
        route_call_tool=route_call_tool,
        require_data_plane_for_http=require_data_plane_for_http,
        app_for_data_plane_project=app_for_data_plane_project,
    )
    for build in (
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
    ):
        http.include_router(build(ctx))

    def list_mcp_tools() -> list[dict[str, Any]]:
        if router is not None:
            return router.list_tools()
        assert api is not None
        return api.app.list_tools()

    def call_mcp_tool(
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        request: Request,
    ) -> dict[str, Any]:
        return route_call_tool(
            name=name,
            arguments=arguments,
            context=context,
            activity_source="mcp",
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )

    register_mcp_routes(
        http,
        list_tools=list_mcp_tools,
        call_tool=call_mcp_tool,
        allow_tool=(
            None
            if surface.allow_data_plane_tool_calls
            else lambda tool: tool.get("plane") != "data"
        ),
    )

    register_data_plane_routes(
        http,
        app_for_project=app_for_data_plane_project,
    )

    # Control-admin routes: optional cleanup trigger plus tenant counters.
    register_admin_routes(
        http,
        store=api.app.store if api is not None else None,
        cleanup=cleanup,
    )

    return http
