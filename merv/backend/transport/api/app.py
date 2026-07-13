"""FastAPI app assembly for the Merv HTTP surface."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from ... import __version__
from ...services.auth import UnauthorizedError
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
    HttpSurfacePolicy,
)
from ..mcp_http import register_mcp_routes
from .context import ApiRouteContext
from .shared import UI_CORS_EXPOSE_HEADERS, UI_CORS_HEADERS, is_local_origin
from .views import ResearchHttpApi
from . import claims, events, experiments, feed, meta, projects, reflections, resources, reviews, sandboxes, sdk_auth, storage


def create_fastapi_app(
    app: Any | None = None,
    *,
    allowed_origins: list[str] | None = None,
    cleanup: Any | None = None,
    surface_policy: HttpSurfacePolicy | None = None,
    auth: Any | None = None,
    ui_base_url: str = "",
) -> FastAPI:
    # Unified HTTP brain. File, SSH, and rsync work is always submitted by the
    # local MCP proxy; the server never accepts repo_root context or data-plane
    # tool calls directly.
    if app is None:
        raise ValueError("provide app")
    surface = surface_policy or HttpSurfacePolicy.for_surface(
        restrict_cors=False,
        hosted_control=False,
    )
    if surface.hosted_control and auth is None:
        raise ValueError("hosted control requires an authentication verifier")
    api = ResearchHttpApi(app=app)

    def api_for_project(project_id: str) -> ResearchHttpApi:
        return api

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

        if context.get("repo_root"):
            raise DataPlaneRequiredError(
                "repo_root context is local data-plane state; hosted control "
                "requires the local MCP proxy to resolve and send project_id",
                details={
                    "field": "context.repo_root",
                    "reason": "repo_root_hidden_from_cloud",
                },
            )
        if name in DATA_PLANE_TOOL_NAMES:
            raise DataPlaneRequiredError(
                f"{name} requires the local MCP proxy; hosted control mode "
                "cannot read local files, hold user SSH keys, or run rsync",
                details={
                    "tool": name,
                    "reason": "requires_local_data_plane",
                },
            )
        # Membership boundary for authenticated (hosted) callers. Non-member
        # projects 404 like nonexistent ones; project/list handlers receive
        # the user id so creation records membership and listing filters.
        user_id = str(getattr(principal, "user_id", "") or "")
        tenant_id = str(getattr(principal, "tenant_id", "") or "")

        def require_member(project_id: str | None) -> None:
            if user_id and project_id and not api.app.store.is_project_member(
                project_id=project_id, user_id=user_id
            ):
                raise NotFoundError(f"project not found: {project_id}")

        require_member(arguments.get("project_id"))
        internal_kwargs: dict[str, Any] = {}
        if user_id and name in ("project", "project.list"):
            internal_kwargs["user_id"] = user_id
        if tenant_id and name == "project":
            internal_kwargs["tenant_id"] = tenant_id
        policy = (
            HOSTED_CONTROL_TOOL_POLICIES.get(name)
            if surface.use_hosted_tool_policies
            else None
        )
        if policy is not None:
            call_kwargs: dict[str, Any] = {}
            if policy.telemetry_from_review_request:
                review_project_id = api.app.reviews.request_project_id(
                    review_request_id=arguments.get("review_request_id")
                )
                # Review tools resolve their project indirectly; gate on the
                # resolved id so a capability can't cross project boundaries.
                require_member(review_project_id)
                call_kwargs["telemetry_project_id"] = review_project_id
            return api.app.call_tool(
                name=name,
                arguments=arguments,
                activity_source=activity_source,
                internal_kwargs=internal_kwargs or None,
                **call_kwargs,
            )
        if name in PROJECT_SCOPED_TOOL_NAMES and "project_id" not in arguments:
            raise ValidationError("project_id is required", details={"field": "project_id"})
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
        return api.app.call_tool(
            name=name,
            arguments=arguments,
            activity_source=activity_source,
            internal_kwargs=internal_kwargs or None,
        )

    http = FastAPI(title="Merv API", version=__version__)

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

    # Membership gate inputs: project-scoped paths carry the id as the segment
    # after /api/projects/; activity + debug are project-scoped via query param.
    project_path = re.compile(r"^/api/projects/([^/]+)")
    query_scoped_prefixes = ("/api/activity", "/api/debug/")

    def _member_gate(request: Request, user_id: str) -> JSONResponse | None:
        # Non-members get the same 404 a nonexistent project would produce.
        path = request.url.path
        match = project_path.match(path)
        if match:
            project_id = match.group(1)
            if not api.app.store.is_project_member(project_id=project_id, user_id=user_id):
                return JSONResponse(
                    {"detail": "project not found", "error_code": "not_found"},
                    status_code=404,
                )
            return None
        if path.startswith(query_scoped_prefixes):
            project_id = request.query_params.get("project_id") or ""
            if not project_id:
                return JSONResponse(
                    {
                        "detail": "project_id is required on this endpoint when authenticated",
                        "error_code": "validation_error",
                    },
                    status_code=400,
                )
            if not api.app.store.is_project_member(project_id=project_id, user_id=user_id):
                return JSONResponse(
                    {"detail": "project not found", "error_code": "not_found"},
                    status_code=404,
                )
        return None

    @http.middleware("http")
    async def attach_principal(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        request.state.principal = LOCAL_PRINCIPAL
        request.state.authenticated = False
        # /health is liveness; /api/meta is the version handshake itself (it
        # also tells the UI whether and how to log in). The MLflow gate route
        # verifies credentials itself with Basic-challenge semantics, and the
        # /api/sdk/auth/* device-flow routes serve clients that are logging in
        # (session ids and refresh tokens are their own credentials).
        if request.url.path in (
            "/health", "/api/meta", "/internal/auth/mlflow"
        ) or request.url.path.startswith("/api/sdk/auth/"):
            return await call_next(request)
        # Version/compat floor (cloud plan Phase 9). A below-floor client is
        # rejected with an actionable upgrade error. A missing header is
        # tolerated for pre-Phase-9 clients. Runs BEFORE auth so an outdated
        # client gets the upgrade message, not a login error.
        client_version = request.headers.get(CLIENT_VERSION_HEADER)
        if surface.hosted_control and client_version and is_below_floor(
            client_version=client_version, floor=MIN_PROXY_VERSION
        ):
            return JSONResponse(
                {
                    "detail": (
                        f"client version {client_version} is below the minimum "
                        f"supported {MIN_PROXY_VERSION}; upgrade the merv "
                        "client (pip install -U merv) and reconnect"
                    ),
                    "error_code": "client_too_old",
                    "min_version": MIN_PROXY_VERSION,
                    "client_version": client_version,
                },
                status_code=426,
            )
        if auth is not None:
            try:
                principal = auth.verify_bearer(request.headers.get("Authorization"))
            except UnauthorizedError as exc:
                return JSONResponse(
                    {
                        "detail": (
                            f"{exc.message}; sign in on the web UI or set an API "
                            "key (merv-client login --api-key rr_sk_...)"
                        ),
                        "error_code": "unauthorized",
                    },
                    status_code=401,
                )
            request.state.principal = principal
            request.state.authenticated = True
            denied = _member_gate(request, principal.user_id)
            if denied is not None:
                return denied
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

    def app_for_data_plane_project(request: Request, project_id: str) -> Any:
        # Data-plane routes carry project_id in the BODY, so the path-based
        # membership gate can't see them; enforce here at their single funnel.
        principal = getattr(request.state, "principal", LOCAL_PRINCIPAL)
        user_id = str(getattr(principal, "user_id", "") or "")
        if user_id and project_id and not api.app.store.is_project_member(
            project_id=project_id, user_id=user_id
        ):
            raise NotFoundError(f"project not found: {project_id}")
        return api_for_project(project_id).app

    if auth is not None:
        # Browser-handoff login for CLI/proxy clients (merv-client login).
        http.include_router(
            sdk_auth.build_router(
                verifier=auth,
                allowed_origins=allowed_origins or [],
                ui_base_url=ui_base_url,
            )
        )

        @http.get("/internal/auth/mlflow")
        def mlflow_gate(request: Request) -> Response:
            # Caddy forward_auth target for the /mlflow reverse-proxy routes.
            # Accepts the same credentials as the API plus HTTP Basic (browser
            # prompt / MLFLOW_TRACKING_USERNAME+PASSWORD pairs put the key in
            # the password slot). 204 admits, 401 challenges.
            try:
                auth.verify_basic_or_bearer(request.headers.get("Authorization"))
            except UnauthorizedError:
                return Response(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="RapidReview MLflow"'},
                )
            return Response(status_code=204)

    ctx = ApiRouteContext(
        api=api,
        surface=surface,
        cleanup=cleanup,
        api_for_project=api_for_project,
        route_call_tool=route_call_tool,
        app_for_data_plane_project=app_for_data_plane_project,
        auth_meta=auth.meta() if auth is not None else None,
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
        allow_tool=lambda tool: tool.get("plane") != "data",
    )

    register_data_plane_routes(
        http,
        app_for_project=app_for_data_plane_project,
    )

    # Control-admin routes: optional cleanup trigger plus tenant counters.
    register_admin_routes(
        http,
        store=api.app.store,
        cleanup=cleanup,
    )

    return http
