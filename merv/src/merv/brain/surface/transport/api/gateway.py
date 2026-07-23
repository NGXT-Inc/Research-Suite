"""Authentication, authorization, and tool invocation for the HTTP surface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from ....kernel.env import mlflow_suspended
from ....kernel.utils import (
    DataPlaneRequiredError,
    NotFoundError,
    ValidationError,
)
from ....kernel.version import CLIENT_VERSION_HEADER, MIN_PROXY_VERSION, is_below_floor
from ...auth import UnauthorizedError
from ...identity import LOCAL_PRINCIPAL, ProjectKeyScopeError, is_local_principal
from ...tools.contracts import TOOL_MANIFEST
from ...tools.tool_facade import ToolDispatcher
from ....research_core.facade import ResearchProjects, ResearchReviewDelivery
from ....sandbox.facade import SandboxFacade
from ..http_policy import HOSTED_CONTROL_TOOL_POLICIES, HttpSurfacePolicy
from .shared import is_local_origin
from . import oauth, project_keys, sdk_auth


@dataclass(frozen=True)
class RequestAuthenticator:
    """Resolve a request principal without coupling the factory to a verifier."""

    surface: HttpSurfacePolicy
    verifier: Any | None = None
    # Phase B: OAuth routes are auth-exempt; audience-bound bearers are /mcp-only.
    oauth_enabled: bool = False
    canonical_mcp_resource: str = ""

    def authenticate(self, request: Request) -> JSONResponse | None:
        if request.method == "OPTIONS":
            return None
        request.state.principal = LOCAL_PRINCIPAL
        request.state.authenticated = False
        path = request.url.path
        if (
            path in ("/health", "/api/meta", "/internal/auth/mlflow")
            or path.startswith(("/api/sdk/auth/", "/api/artifacts/u/", "/api/artifacts/f/"))
            or oauth.public_request(request, enabled=self.oauth_enabled)
        ):
            return None
        client_version = request.headers.get(CLIENT_VERSION_HEADER)
        if (
            self.surface.hosted_control
            and client_version
            and is_below_floor(client_version=client_version, floor=MIN_PROXY_VERSION)
        ):
            return JSONResponse(
                {"detail": f"client version {client_version} is below the minimum "
                 f"supported {MIN_PROXY_VERSION}; upgrade the merv client "
                 "(pip install -U merv) and reconnect",
                 "error_code": "client_too_old",
                 "min_version": MIN_PROXY_VERSION,
                 "client_version": client_version},
                status_code=426,
            )
        if self.verifier is None:
            return None
        try:
            principal = self.verifier.verify_bearer(request.headers.get("Authorization"))
        except UnauthorizedError as exc:
            return oauth.bearer_denial(request, message=exc.message,
                                       enabled=self.oauth_enabled, session_denial=None)
        request.state.principal = principal
        request.state.authenticated = True
        # INV-7: audience-bound bearers are valid ONLY on the canonical /mcp path.
        return oauth.credential_audience_denial(request=request, principal=principal,
                                                canonical_mcp_resource=self.canonical_mcp_resource)


@dataclass(frozen=True)
class ProjectAuthorizer:
    """The single project-membership boundary for every HTTP entry path."""

    projects: ResearchProjects
    _project_path = re.compile(r"^/api/projects/([^/]+)")
    _query_scoped_prefixes = ("/api/activity", "/api/debug/")
    # Operator/tenant diagnostics an mk_ key must never reach (INV-11).
    _operator_diagnostic_prefixes = ("/api/activity", "/api/debug/", "/api/admin")

    @staticmethod
    def user_id(principal: Any) -> str:
        return str(getattr(principal, "user_id", "") or "")

    @staticmethod
    def key_project_id(principal: Any) -> str:
        return str(getattr(principal, "key_project_id", "") or "")

    def require_key_scope(self, *, project_id: str | None, principal: Any) -> None:
        """Exact key-project equality, BEFORE any membership check (INV-11)."""
        key_project_id = self.key_project_id(principal)
        if key_project_id and project_id and project_id != key_project_id:
            raise ProjectKeyScopeError(
                "project API key cannot access a different project",
                details={"key_project_id": key_project_id, "requested_project_id": project_id},
            )

    def require_member(self, *, project_id: str | None, principal: Any) -> None:
        self.require_key_scope(project_id=project_id, principal=principal)
        user_id = self.user_id(principal)
        if (
            user_id
            and project_id
            and not self.projects.is_member(project_id=project_id, user_id=user_id)
        ):
            raise NotFoundError(f"project not found: {project_id}")

    def http_denial(self, request: Request) -> JSONResponse | None:
        path = request.url.path
        if self.key_project_id(request.state.principal) and path.startswith(
            self._operator_diagnostic_prefixes
        ):
            return JSONResponse(
                {"detail": "project API keys cannot access operator diagnostics",
                 "error_code": "project_scope_forbidden"},
                status_code=403,
            )
        match = self._project_path.match(path)
        project_id = match.group(1) if match else ""
        if not project_id and path.startswith(self._query_scoped_prefixes):
            project_id = request.query_params.get("project_id") or ""
            if not project_id:
                return JSONResponse(
                    {"detail": "project_id is required on this endpoint when authenticated",
                     "error_code": "validation_error"},
                    status_code=400,
                )
        try:
            self.require_key_scope(project_id=project_id, principal=request.state.principal)
        except ProjectKeyScopeError as exc:
            return JSONResponse(
                {"detail": exc.message, "error_code": exc.error_code, **exc.details},
                status_code=403,
            )
        if project_id and not self.projects.is_member(
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

    tools: ToolDispatcher
    reviews: ResearchReviewDelivery
    sandboxes: SandboxFacade
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
        base_url: str = "",  # renders the artifact.submit upload one-liner
    ) -> dict[str, Any]:
        arguments = dict(arguments or {})
        context = dict(context or {})
        contract = TOOL_MANIFEST.get(name)
        # INV-5: an MCP call from any non-local principal (mk_/rr_sk_/JWT) is
        # confined to public tools by the dispatcher; local composition is not.
        caller_is_external_mcp = activity_source == "mcp" and not is_local_principal(
            principal
        )
        if context.get("repo_root"):
            raise DataPlaneRequiredError(
                "repo_root context is local data-plane state; hosted control "
                "requires the local MCP proxy to resolve and send project_id",
                details={"field": "context.repo_root",
                         "reason": "repo_root_hidden_from_cloud"},
            )
        if contract is not None and contract.plane == "data":
            raise DataPlaneRequiredError(
                f"{name} requires the local MCP proxy; hosted control mode cannot read "
                "local files, hold user SSH keys, or run rsync",
                details={"tool": name, "reason": "requires_local_data_plane"},
            )
        for scope in (arguments.get("project_id"), project_scope):
            self.projects.require_member(project_id=scope, principal=principal)
        user_id = self.projects.user_id(principal)
        key_project_id = self.projects.key_project_id(principal)
        if key_project_id and name == "project" and arguments.get("action") == "create":
            raise ProjectKeyScopeError("project API keys cannot create projects",
                                       details={"key_project_id": key_project_id})
        internal_kwargs = None
        if user_id and name in ("project", "project.list"):
            internal_kwargs = {"user_id": user_id}
            if key_project_id and name == "project.list":
                internal_kwargs["project_id"] = key_project_id  # bound project only
        if name == "artifact.submit" and base_url:
            internal_kwargs = {"base_url": base_url}
        policy = (
            HOSTED_CONTROL_TOOL_POLICIES.get(name)
            if self.surface.use_hosted_tool_policies
            else None
        )
        call_kwargs: dict[str, Any] = {"caller_is_external_mcp": caller_is_external_mcp}
        if project_scope:
            call_kwargs["telemetry_project_id"] = project_scope
        if policy is not None:
            if policy.telemetry_from_review_request:
                project_id = self.reviews.request_project_id(
                    review_request_id=arguments.get("review_request_id")
                )
                self.projects.require_member(project_id=project_id, principal=principal)
                if project_scope and project_id != project_scope:
                    raise NotFoundError(f"project not found: {project_scope}")
                call_kwargs["telemetry_project_id"] = project_id
            if policy.telemetry_from_review_session:
                # INV-9: the session's own project decides scope, so an mk_ key
                # cannot ride a foreign session id into another project.
                project_id = self.reviews.session_project_id(
                    review_session_id=arguments.get("review_session_id")
                )
                self.projects.require_member(project_id=project_id, principal=principal)
                if project_scope and project_id != project_scope:
                    raise NotFoundError(f"project not found: {project_scope}")
                call_kwargs["telemetry_project_id"] = project_id
            return self.tools.call_tool(
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
            return self.sandboxes.get(
                experiment_id=request.experiment_id,
                project_id=request.project_id,
                tenant_id=None,
                sandbox_uid=request.sandbox_uid,
                include_data_plane_enrichment=False,
            )
        return self.tools.call_tool(
            name=name,
            arguments=arguments,
            activity_source=activity_source,
            internal_kwargs=internal_kwargs,
            **call_kwargs,
        )

    def call_mcp(
        self, name: str, arguments: dict[str, Any],
        context: dict[str, Any], request: Request,
    ) -> dict[str, Any]:
        return self.call(
            name=name,
            arguments=arguments,
            context=context,
            activity_source="mcp",
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
            base_url=str(request.base_url).rstrip("/"),  # caller-reachable base
        )

    def authorize_data_plane_project(self, request: Request, project_id: str) -> None:
        self.projects.require_member(
            project_id=project_id,
            principal=getattr(request.state, "principal", LOCAL_PRINCIPAL),
        )


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
                {"detail": "cross-origin requests to the local HTTP server are not allowed",
                 "error_code": "forbidden_origin"},
                status_code=403,
            )
        return await call_next(request)

    @http.middleware("http")
    async def attach_principal(request: Request, call_next):
        denied = authenticator.authenticate(request)
        if denied is None and getattr(request.state, "authenticated", False):
            denied = authorizer.http_denial(request)
        return denied if denied is not None else await call_next(request)


def install_auth_routes(
    http: FastAPI,
    *,
    verifier: Any | None,
    allowed_origins: list[str] | None,
    ui_base_url: str,
    owner_key_audience: str = "",
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
    if getattr(verifier, "project_keys", None) is not None:
        http.include_router(
            project_keys.build_router(
                keys=verifier.project_keys, audience=owner_key_audience
            )
        )

    @http.get("/internal/auth/mlflow")
    def mlflow_gate(request: Request) -> Response:
        if mlflow_suspended():  # ruling 3: no principal passes while suspended
            return JSONResponse(
                {"detail": "MLflow is temporarily suspended",
                 "error_code": "mlflow_suspended"}, status_code=403)
        try:
            principal = verifier.verify_basic_or_bearer(
                request.headers.get("Authorization")
            )
        except UnauthorizedError:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="RapidReview MLflow"'},
            )
        # A project (mk_) key is not a valid MLflow-audience credential (INV-7).
        if getattr(principal, "key_id", None):
            return JSONResponse(
                {"detail": "project API keys are not valid for the MLflow audience",
                 "error_code": "credential_audience_forbidden"},
                status_code=403,
            )
        return Response(status_code=204)
