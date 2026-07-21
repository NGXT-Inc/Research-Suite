"""Authentication, authorization, and tool invocation for the HTTP surface."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from ....kernel.state import monotonic_ms
from ....kernel.utils import (
    ContentUnavailableError,
    DataPlaneRequiredError,
    NotFoundError,
    ResearchPluginError,
    ValidationError,
)
from ....kernel.version import CLIENT_VERSION_HEADER, MIN_PROXY_VERSION, is_below_floor
from ...auth import UnauthorizedError
from ...identity import LOCAL_PRINCIPAL
from ...tools.contracts import TOOL_MANIFEST
from ..http_policy import HOSTED_CONTROL_TOOL_POLICIES, HttpSurfacePolicy
from .shared import UI_CORS_EXPOSE_HEADERS, UI_CORS_HEADERS, is_local_origin
from . import sdk_auth


@dataclass(frozen=True)
class RequestAuthenticator:
    """Resolve a request principal without coupling the factory to a verifier."""

    surface: HttpSurfacePolicy
    verifier: Any | None = None

    def authenticate(self, request: Request) -> JSONResponse | None:
        if request.method == "OPTIONS":
            return None
        request.state.principal = LOCAL_PRINCIPAL
        request.state.authenticated = False
        path = request.url.path
        if path in ("/health", "/api/meta", "/internal/auth/mlflow") or path.startswith(
            "/api/sdk/auth/"
        ):
            return None
        client_version = request.headers.get(CLIENT_VERSION_HEADER)
        if (
            self.surface.hosted_control
            and client_version
            and is_below_floor(client_version=client_version, floor=MIN_PROXY_VERSION)
        ):
            return JSONResponse(
                {
                    "detail": f"client version {client_version} is below the minimum supported "
                    f"{MIN_PROXY_VERSION}; upgrade the merv client (pip install -U merv) and reconnect",
                    "error_code": "client_too_old",
                    "min_version": MIN_PROXY_VERSION,
                    "client_version": client_version,
                },
                status_code=426,
            )
        if self.verifier is None:
            return None
        try:
            principal = self.verifier.verify_bearer(
                request.headers.get("Authorization")
            )
        except UnauthorizedError as exc:
            return JSONResponse(
                {
                    "detail": f"{exc.message}; sign in on the web UI or set an API key "
                    "(merv-client login --api-key rr_sk_...)",
                    "error_code": "unauthorized",
                },
                status_code=401,
            )
        request.state.principal = principal
        request.state.authenticated = True
        return None


@dataclass(frozen=True)
class ProjectAuthorizer:
    """The single project-membership boundary for every HTTP entry path."""

    member_lookup: Callable[..., bool]
    _project_path = re.compile(r"^/api/projects/([^/]+)")
    _query_scoped_prefixes = ("/api/activity", "/api/debug/")

    @staticmethod
    def user_id(principal: Any) -> str:
        return str(getattr(principal, "user_id", "") or "")

    def _is_member(self, *, project_id: str, user_id: str) -> bool:
        return self.member_lookup(project_id=project_id, user_id=user_id)

    def require_member(self, *, project_id: str | None, principal: Any) -> None:
        user_id = self.user_id(principal)
        if (
            user_id
            and project_id
            and not self._is_member(project_id=project_id, user_id=user_id)
        ):
            raise NotFoundError(f"project not found: {project_id}")

    def http_denial(self, request: Request) -> JSONResponse | None:
        path = request.url.path
        match = self._project_path.match(path)
        project_id = match.group(1) if match else ""
        if not project_id and path.startswith(self._query_scoped_prefixes):
            project_id = request.query_params.get("project_id") or ""
            if not project_id:
                return JSONResponse(
                    {
                        "detail": "project_id is required on this endpoint when authenticated",
                        "error_code": "validation_error",
                    },
                    status_code=400,
                )
        if project_id and not self._is_member(
            project_id=project_id, user_id=self.user_id(request.state.principal)
        ):
            return JSONResponse(
                {"detail": "project not found", "error_code": "not_found"},
                status_code=404,
            )
        return None


@dataclass(frozen=True)
class ToolInvocationGateway:
    """Apply hosted-tool policy before delegating to application commands."""

    backend: Any
    surface: HttpSurfacePolicy
    projects: ProjectAuthorizer

    def call(
        self,
        *,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        project_scope: str | None = None,
        activity_source: str = "http",
        principal: Any | None = None,
    ) -> dict[str, Any]:
        arguments = dict(arguments or {})
        context = dict(context or {})
        contract = TOOL_MANIFEST.get(name)
        if context.get("repo_root"):
            raise DataPlaneRequiredError(
                "repo_root context is local data-plane state; hosted control "
                "requires the local MCP proxy to resolve and send project_id",
                details={
                    "field": "context.repo_root",
                    "reason": "repo_root_hidden_from_cloud",
                },
            )
        if contract is not None and contract.plane == "data":
            raise DataPlaneRequiredError(
                f"{name} requires the local MCP proxy; hosted control mode cannot read "
                "local files, hold user SSH keys, or run rsync",
                details={"tool": name, "reason": "requires_local_data_plane"},
            )
        self.projects.require_member(
            project_id=arguments.get("project_id"), principal=principal
        )
        self.projects.require_member(project_id=project_scope, principal=principal)
        user_id = self.projects.user_id(principal)
        internal_kwargs = (
            {"user_id": user_id}
            if user_id and name in ("project", "project.list")
            else None
        )
        policy = (
            HOSTED_CONTROL_TOOL_POLICIES.get(name)
            if self.surface.use_hosted_tool_policies
            else None
        )
        call_kwargs = {"telemetry_project_id": project_scope} if project_scope else {}
        if policy is not None:
            if policy.telemetry_from_review_request:
                project_id = self.backend.reviews.request_project_id(
                    review_request_id=arguments.get("review_request_id")
                )
                self.projects.require_member(project_id=project_id, principal=principal)
                if project_scope and project_id != project_scope:
                    raise NotFoundError(f"project not found: {project_scope}")
                call_kwargs["telemetry_project_id"] = project_id
            return self.backend.call_tool(
                name=name,
                arguments=arguments,
                activity_source=activity_source,
                internal_kwargs=internal_kwargs,
                **call_kwargs,
            )
        if (
            contract is not None
            and contract.scope_strategy == "linked-project"
            and "project_id" not in arguments
        ):
            raise ValidationError(
                "project_id is required", details={"field": "project_id"}
            )
        if (
            self.surface.hosted_control
            and contract is not None
            and contract.hosted_control_sandbox_lookup
        ):
            try:
                request = contract.input_model.model_validate(arguments)
            except PydanticValidationError as exc:
                raise ValidationError(
                    "invalid tool arguments",
                    details={"tool": name, "errors": exc.errors()},
                ) from exc
            return self.backend.sandboxes.get(
                experiment_id=request.experiment_id,
                project_id=request.project_id,
                tenant_id=None,
                sandbox_uid=request.sandbox_uid,
                include_data_plane_enrichment=False,
            )
        return self.backend.call_tool(
            name=name,
            arguments=arguments,
            activity_source=activity_source,
            internal_kwargs=internal_kwargs,
            **call_kwargs,
        )

    def call_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        request: Request,
    ) -> dict[str, Any]:
        return self.call(
            name=name,
            arguments=arguments,
            context=context,
            activity_source="mcp",
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )

    def app_for_data_plane_project(self, request: Request, project_id: str) -> Any:
        self.projects.require_member(
            project_id=project_id,
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )
        return self.backend


def install_request_middleware(
    http: FastAPI, *, authenticator: RequestAuthenticator, authorizer: ProjectAuthorizer
) -> None:
    @http.middleware("http")
    async def reject_foreign_origins(request: Request, call_next):
        origin = request.headers.get("origin")
        if (
            not authenticator.surface.restrict_cors
            and not authenticator.surface.hosted_control
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
        denied = authenticator.authenticate(request)
        if denied is None and getattr(request.state, "authenticated", False):
            denied = authorizer.http_denial(request)
        return denied if denied is not None else await call_next(request)


def install_activity_middleware(http: FastAPI, *, structured_logger: Any) -> None:
    @http.middleware("http")
    async def log_http_activity(request: Request, call_next):
        started = monotonic_ms()
        status = 500
        request_id = uuid.uuid4().hex[:16]
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-RP-Request-Id"] = request_id
            return response
        finally:
            principal = getattr(request.state, "principal", None)
            structured_logger.log(
                kind="http",
                request_id=request_id,
                tenant_id=getattr(principal, "tenant_id", "") or "",
                path=str(request.url.path),
                status=status,
                duration_ms=monotonic_ms() - started,
                method=request.method,
            )


def install_cors(
    http: FastAPI, *, allowed_origins: list[str] | None, surface: HttpSurfacePolicy
) -> None:
    http.add_middleware(
        CORSMiddleware,
        allow_origins=(allowed_origins or []) if surface.restrict_cors else ["*"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=UI_CORS_HEADERS,
        expose_headers=UI_CORS_EXPOSE_HEADERS,
    )


def install_error_handlers(http: FastAPI) -> None:
    @http.exception_handler(ResearchPluginError)
    async def research_error_handler(
        _request: Request, exc: ResearchPluginError
    ) -> JSONResponse:
        status = (
            404 if isinstance(exc, (NotFoundError, ContentUnavailableError)) else 400
        )
        return JSONResponse(
            {"detail": exc.message, "error_code": exc.error_code, **exc.details},
            status_code=status,
        )

    @http.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "invalid HTTP request", "errors": exc.errors()}, status_code=400
        )


def install_auth_routes(
    http: FastAPI,
    *,
    verifier: Any | None,
    allowed_origins: list[str] | None,
    ui_base_url: str,
) -> None:
    if verifier is None:
        return
    http.include_router(
        sdk_auth.build_router(
            verifier=verifier,
            allowed_origins=allowed_origins or [],
            ui_base_url=ui_base_url,
        )
    )

    @http.get("/internal/auth/mlflow")
    def mlflow_gate(request: Request) -> Response:
        try:
            verifier.verify_basic_or_bearer(request.headers.get("Authorization"))
        except UnauthorizedError:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="RapidReview MLflow"'},
            )
        return Response(status_code=204)
