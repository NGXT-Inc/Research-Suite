"""OAuth discovery, DCR, authorization, consent, and token HTTP endpoints."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...oauth import OAuthControl, OAuthError, oauth_error_redirect
from ..request_body import RequestBodyTooLarge, read_limited_body

_NO_STORE = dict((("Cache-Control", "no-store"), ("Pra" + "gma", "no-cache")))
_MAX_DCR_BODY_BYTES = 32 * 1024
_MAX_TOKEN_BODY_BYTES = 8 * 1024


def install_routes(
    http: FastAPI,
    *,
    service: OAuthControl | None,
    allowed_origins: list[str],
    ui_base_url: str,
    canonical_mcp_resource: str,
) -> None:
    if service is not None:
        http.include_router(
            build_router(
                service=service,
                allowed_origins=allowed_origins,
                ui_base_url=ui_base_url,
                canonical_mcp_resource=canonical_mcp_resource,
            )
        )


def public_request(request: Request, *, enabled: bool) -> bool:
    if not enabled:
        return False
    path = request.url.path
    return path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
        "/oauth/register",
        "/oauth/token",
    ) or path == "/oauth/authorize" and request.method == "GET"


def challenge_denial(
    request: Request, response: JSONResponse, *, enabled: bool
) -> JSONResponse:
    if enabled and request.url.path == "/mcp":
        metadata = protected_resource_metadata_url(request)
        response.headers["WWW-Authenticate"] = (
            f'Bearer resource_metadata="{metadata}"'
        )
    return response


def bearer_denial(
    request: Request,
    *,
    message: str,
    enabled: bool,
    session_denial: JSONResponse | None,
) -> JSONResponse:
    response = session_denial or JSONResponse(
        {
            "detail": f"{message}; sign in on the web UI or authenticate with "
            "a bearer key (an mk_ project key or rr_sk_ API key)",
            "error_code": "unauthorized",
        },
        status_code=401,
    )
    return challenge_denial(request, response, enabled=enabled)


def credential_audience_denial(
    *, request: Request, principal: object, canonical_mcp_resource: str
) -> JSONResponse | None:
    """Require an audience-bound bearer on exactly its configured resource."""
    audience = str(getattr(principal, "audience", "") or "")
    if not audience:
        return None
    path = request.url.path
    current_resource = f"{_origin(request)}/mcp"
    if (
        (path == "/mcp" or path.startswith("/mcp/"))
        and audience == canonical_mcp_resource
        and current_resource == canonical_mcp_resource
    ):
        return None
    return JSONResponse(
        {
            "detail": "credential is restricted to the MCP audience",
            "error_code": "credential_audience_forbidden",
        },
        status_code=403,
    )


def build_router(
    *,
    service: OAuthControl,
    allowed_origins: list[str],
    ui_base_url: str,
    canonical_mcp_resource: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/.well-known/oauth-authorization-server")
    def authorization_server_metadata(request: Request) -> dict[str, Any]:
        origin = _origin(request)
        return {
            "issuer": origin,
            "authorization_endpoint": f"{origin}/oauth/authorize",
            "token_endpoint": f"{origin}/oauth/token",
            "registration_endpoint": f"{origin}/oauth/register",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
            "protected_resources": [canonical_mcp_resource],
        }

    @router.get("/.well-known/oauth-protected-resource/mcp")
    def protected_resource_metadata(request: Request) -> dict[str, Any]:
        origin = _origin(request)
        return {
            "resource": canonical_mcp_resource,
            "authorization_servers": [origin],
            "bearer_methods_supported": ["header"],
        }

    @router.post("/oauth/register", status_code=201)
    async def register(request: Request) -> JSONResponse:
        if _content_type(request) != "application/json":
            return _dcr_error(
                OAuthError(
                    "invalid_client_metadata", "Content-Type must be application/json"
                )
            )
        try:
            body = await read_limited_body(request, limit=_MAX_DCR_BODY_BYTES)
        except RequestBodyTooLarge:
            return _dcr_error(
                OAuthError(
                    "invalid_client_metadata", "registration request is too large"
                )
            )
        try:
            metadata = json.loads(body)
        except Exception:
            return _dcr_error(
                OAuthError(
                    "invalid_client_metadata", "request body must be a JSON object"
                )
            )
        if not isinstance(metadata, dict):
            return _dcr_error(
                OAuthError(
                    "invalid_client_metadata", "request body must be a JSON object"
                )
            )
        try:
            result = service.register_client(metadata)
        except OAuthError as exc:
            return _dcr_error(exc)
        return JSONResponse(result, status_code=201, headers=_NO_STORE)

    @router.get("/oauth/authorize")
    def begin_authorization(request: Request):
        issuer = _origin(request)
        try:
            params = _unique_query(request)
            service.authorization_details(
                params=params, canonical_resource=canonical_mcp_resource
            )
        except OAuthError as exc:
            return _authorization_error(exc=exc, issuer=issuer)
        ui = _ui_origin(
            request,
            allowed_origins=allowed_origins,
            ui_base_url=ui_base_url,
        )
        if ui == issuer:
            return _authorization_error(
                exc=OAuthError(
                    "server_error", "OAuth consent UI base URL is not configured"
                ),
                issuer=issuer,
                status_code=503,
            )
        target = f"{ui}/oauth/authorize"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=302, headers=_NO_STORE)

    @router.get("/oauth/authorize/details")
    def authorization_details(request: Request):
        denial = _require_supabase_session(request)
        if denial is not None:
            return denial
        issuer = _origin(request)
        try:
            result = service.authorization_details(
                params=_unique_query(request),
                canonical_resource=canonical_mcp_resource,
            )
        except OAuthError as exc:
            return _oauth_json_error(exc)
        return JSONResponse(result, headers=_NO_STORE)

    @router.post("/oauth/authorize")
    async def complete_authorization(request: Request):
        denial = _require_supabase_session(request)
        if denial is not None:
            return denial
        if _content_type(request) != "application/json":
            return _oauth_json_error(
                OAuthError("invalid_request", "Content-Type must be application/json")
            )
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return _oauth_json_error(
                OAuthError("invalid_request", "request body must be a JSON object")
            )
        decision = str(body.pop("decision", ""))
        project_id = str(body.pop("project_id", ""))
        if decision not in ("approve", "deny") or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in body.items()
        ):
            return _oauth_json_error(
                OAuthError("invalid_request", "invalid consent decision")
            )
        issuer = _origin(request)
        try:
            redirect_to = service.authorize(
                params=body,
                canonical_resource=canonical_mcp_resource,
                issuer=issuer,
                owner_user_id=_session_owner(request),
                project_id=project_id,
                approved=decision == "approve",
            )
        except OAuthError as exc:
            return _oauth_json_error(exc)
        return JSONResponse({"redirect_to": redirect_to}, headers=_NO_STORE)

    @router.post("/oauth/token")
    async def token(request: Request):
        authorization = request.headers.get("Authorization")
        if authorization:
            # RFC 6749 §5.2: a client that attempted to authenticate via the
            # Authorization header must get 401 with a matching challenge.
            scheme = authorization.split(" ", 1)[0] or "Basic"
            return _client_auth_error(
                OAuthError("invalid_client", "public clients must not authenticate"),
                scheme=scheme,
            )
        if _content_type(request) != "application/x-www-form-urlencoded":
            return _token_error(
                OAuthError(
                    "invalid_request",
                    "Content-Type must be application/x-www-form-urlencoded",
                )
            )
        try:
            body = await read_limited_body(request, limit=_MAX_TOKEN_BODY_BYTES)
        except RequestBodyTooLarge:
            return _token_error(
                OAuthError("invalid_request", "token request is too large")
            )
        try:
            pairs = parse_qsl(
                body.decode("utf-8"), keep_blank_values=True, strict_parsing=True
            )
        except (UnicodeDecodeError, ValueError):
            return _token_error(
                OAuthError("invalid_request", "malformed token request")
            )
        if len({key for key, _value in pairs}) != len(pairs):
            return _token_error(
                OAuthError(
                    "invalid_request", "duplicate token parameters are not allowed"
                )
            )
        form = dict(pairs)
        grant_type = form.get("grant_type")
        try:
            if grant_type == "authorization_code":
                result = service.exchange_code(
                    form=form, canonical_resource=canonical_mcp_resource
                )
            elif grant_type == "refresh_token":
                result = service.refresh(
                    form=form, canonical_resource=canonical_mcp_resource
                )
            else:
                raise OAuthError(
                    "unsupported_grant_type", "grant_type is not supported"
                )
        except OAuthError as exc:
            return _token_error(exc)
        return JSONResponse(result, headers=_NO_STORE)

    return router


def protected_resource_metadata_url(request: Request) -> str:
    return f"{_origin(request)}/.well-known/oauth-protected-resource/mcp"


def _origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _ui_origin(
    request: Request, *, allowed_origins: list[str], ui_base_url: str
) -> str:
    if ui_base_url:
        return ui_base_url.rstrip("/")
    if allowed_origins:
        return allowed_origins[0].rstrip("/")
    return _origin(request)


def _content_type(request: Request) -> str:
    return request.headers.get("Content-Type", "").partition(";")[0].strip().lower()


def _unique_query(request: Request) -> dict[str, str]:
    pairs = list(request.query_params.multi_items())
    if len({key for key, _value in pairs}) != len(pairs):
        raise OAuthError("invalid_request", "duplicate authorization parameters")
    return dict(pairs)


def _session_owner(request: Request) -> str:
    return str(getattr(request.state.principal, "user_id", "") or "")


def _require_supabase_session(request: Request) -> JSONResponse | None:
    principal = getattr(request.state, "principal", None)
    if not str(getattr(principal, "client_id", "")).startswith("jwt:"):
        return JSONResponse(
            {
                "error": "access_denied",
                "error_description": "a Supabase browser session is required",
            },
            status_code=401,
            headers=_NO_STORE,
        )
    return None


def _authorization_error(*, exc: OAuthError, issuer: str, status_code: int = 400):
    redirect = oauth_error_redirect(exc=exc, issuer=issuer)
    if redirect is not None:
        return RedirectResponse(redirect, status_code=302, headers=_NO_STORE)
    return _oauth_json_error(exc, status_code=status_code)


def _oauth_json_error(exc: OAuthError, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": exc.error, "error_description": exc.description},
        status_code=status_code,
        headers=_NO_STORE,
    )


def _dcr_error(exc: OAuthError) -> JSONResponse:
    return _oauth_json_error(exc)


def _token_error(exc: OAuthError) -> JSONResponse:
    return _oauth_json_error(exc)


def _client_auth_error(exc: OAuthError, *, scheme: str) -> JSONResponse:
    response = _oauth_json_error(exc, status_code=401)
    response.headers["WWW-Authenticate"] = scheme
    return response


__all__ = [
    "build_router",
    "bearer_denial",
    "challenge_denial",
    "credential_audience_denial",
    "install_routes",
    "protected_resource_metadata_url",
    "public_request",
]
