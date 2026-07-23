"""Minimal OAuth 2.1 policy for project-scoped MCP credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..kernel.secret_tokens import hash_secret, mint_secret, secret_digest_matches
from ..kernel.utils import NotFoundError, iso_after, new_id, now_iso, parse_iso
from .project_keys import ProjectKeyControl

AUTHORIZATION_CODE_TTL_SECONDS = 60
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60

_PKCE_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_SUPPORTED_GRANTS = frozenset(("authorization_code", "refresh_token"))


class OAuthError(Exception):
    """Protocol error safe to return from an OAuth endpoint."""

    def __init__(
        self,
        error: str,
        description: str,
        *,
        redirect_uri: str | None = None,
        state: str | None = None,
    ) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.redirect_uri = redirect_uri
        self.state = state


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    grant_types: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class AuthorizationCode:
    code_digest: str
    client_id: str
    redirect_uri: str
    owner_user_id: str
    project_id: str
    code_challenge: str
    resource: str
    created_at: str
    expires_at: str
    consumed_at: str | None


@dataclass(frozen=True, slots=True)
class RefreshToken:
    id: str
    family_id: str
    secret_digest: str
    client_id: str
    owner_user_id: str
    project_id: str
    resource: str
    current_key_id: str
    parent_token_id: str | None
    created_at: str
    expires_at: str
    consumed_at: str | None
    revoked_at: str | None


class OAuthRepository(Protocol):
    def insert_client(self, *, client: OAuthClient) -> None: ...
    def client_by_id(self, *, client_id: str) -> OAuthClient | None: ...
    def insert_code(self, *, code: AuthorizationCode) -> None: ...
    def code_by_digest(self, *, digest: str) -> AuthorizationCode | None: ...
    def consume_code(self, *, digest: str, consumed_at: str) -> bool: ...
    def insert_refresh_token(self, *, token: RefreshToken) -> None: ...
    def refresh_token_by_digest(self, *, digest: str) -> RefreshToken | None: ...
    def consume_refresh_token(self, *, token_id: str, consumed_at: str) -> bool: ...
    def revoke_refresh_family_and_key_lineage(
        self,
        *,
        family_id: str,
        key_id: str,
        project_id: str,
        owner_user_id: str,
        revoked_at: str,
    ) -> None: ...


class OAuthControl(Protocol):
    def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def authorization_details(self, **kwargs: object) -> dict[str, Any]: ...
    def authorize(self, **kwargs: object) -> str: ...
    def exchange_code(self, **kwargs: object) -> dict[str, Any]: ...
    def refresh(self, **kwargs: object) -> dict[str, Any]: ...


class ProjectMembership(Protocol):
    def __call__(self, *, project_id: str, user_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    client: OAuthClient
    redirect_uri: str
    state: str | None
    code_challenge: str
    resource: str


class OAuthService:
    """DCR, authorization-code, and refresh-rotation application policy."""

    def __init__(
        self,
        *,
        repository: OAuthRepository,
        project_keys: ProjectKeyControl,
        is_project_member: ProjectMembership,
    ) -> None:
        self._repository = repository
        self._project_keys = project_keys
        self._is_project_member = is_project_member

    def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        raw_name = metadata.get("client_name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not name or len(name) > 200 or _has_control_character(name):
            raise OAuthError(
                "invalid_client_metadata",
                "client_name is required and must be at most 200 printable characters",
            )
        redirect_uris = _string_list(
            metadata.get("redirect_uris"), field="redirect_uris", required=True
        )
        if len(redirect_uris) > 10:
            raise OAuthError(
                "invalid_redirect_uri", "at most 10 redirect_uris may be registered"
            )
        for uri in redirect_uris:
            if not valid_redirect_uri(uri):
                raise OAuthError(
                    "invalid_redirect_uri",
                    "redirect_uris must be exact HTTPS URLs or HTTP localhost URLs",
                )
        if metadata.get("token_endpoint_auth_method") != "none":
            raise OAuthError(
                "invalid_client_metadata",
                "only public clients with token_endpoint_auth_method=none are supported",
            )
        grants = _string_list(
            metadata.get("grant_types", ["authorization_code"]),
            field="grant_types",
            required=True,
        )
        if "authorization_code" not in grants or not set(grants) <= _SUPPORTED_GRANTS:
            raise OAuthError(
                "invalid_client_metadata",
                "grant_types may contain only authorization_code and refresh_token",
            )
        response_types = _string_list(
            metadata.get("response_types", ["code"]),
            field="response_types",
            required=True,
        )
        if response_types != ("code",):
            raise OAuthError(
                "invalid_client_metadata", 'only response_types=["code"] is supported'
            )
        if str(metadata.get("scope") or "").strip():
            raise OAuthError(
                "invalid_client_metadata", "registered scopes are not supported"
            )
        client = OAuthClient(
            client_id=new_id(prefix="oauthc"),
            client_name=name,
            redirect_uris=redirect_uris,
            grant_types=grants,
            created_at=now_iso(),
        )
        self._repository.insert_client(client=client)
        issued = parse_iso(client.created_at)
        return {
            "client_id": client.client_id,
            "client_id_issued_at": int(issued.timestamp()) if issued else 0,
            "client_name": client.client_name,
            "redirect_uris": list(client.redirect_uris),
            "token_endpoint_auth_method": "none",
            "grant_types": list(client.grant_types),
            "response_types": ["code"],
        }

    def authorization_details(
        self, *, params: dict[str, str], canonical_resource: str
    ) -> dict[str, Any]:
        request = self._authorization_request(
            params=params, canonical_resource=canonical_resource
        )
        return {
            "client_id": request.client.client_id,
            "client_name": request.client.client_name,
            "resource": request.resource,
        }

    def authorize(
        self,
        *,
        params: dict[str, str],
        canonical_resource: str,
        issuer: str,
        owner_user_id: str,
        project_id: str,
        approved: bool,
    ) -> str:
        request = self._authorization_request(
            params=params, canonical_resource=canonical_resource
        )
        if not approved:
            return authorization_redirect(
                redirect_uri=request.redirect_uri,
                issuer=issuer,
                state=request.state,
                error="access_denied",
            )
        if (
            not project_id
            or not owner_user_id
            or not self._is_project_member(project_id=project_id, user_id=owner_user_id)
        ):
            return authorization_redirect(
                redirect_uri=request.redirect_uri,
                issuer=issuer,
                state=request.state,
                error="access_denied",
            )
        secret = mint_secret(prefix="mac_", nbytes=32)
        self._repository.insert_code(
            code=AuthorizationCode(
                code_digest=hash_secret(secret),
                client_id=request.client.client_id,
                redirect_uri=request.redirect_uri,
                owner_user_id=owner_user_id,
                project_id=project_id,
                code_challenge=request.code_challenge,
                resource=request.resource,
                created_at=now_iso(),
                expires_at=iso_after(seconds=AUTHORIZATION_CODE_TTL_SECONDS),
                consumed_at=None,
            )
        )
        return authorization_redirect(
            redirect_uri=request.redirect_uri,
            issuer=issuer,
            state=request.state,
            code=secret,
        )

    def exchange_code(
        self, *, form: dict[str, str], canonical_resource: str
    ) -> dict[str, Any]:
        client = self._token_client(form)
        if "authorization_code" not in client.grant_types:
            raise OAuthError(
                "unauthorized_client", "client cannot use authorization_code"
            )
        raw_code = _required_form(form, "code")
        digest = hash_secret(raw_code)
        code = self._repository.code_by_digest(digest=digest)
        if not secret_digest_matches(
            stored_digest=code.code_digest if code else None,
            presented_digest=digest,
        ):
            raise OAuthError("invalid_grant", "authorization code is invalid")
        assert code is not None
        verifier = _required_form(form, "code_verifier")
        if (
            code.client_id != client.client_id
            or code.redirect_uri != _required_form(form, "redirect_uri")
            or code.resource != _required_resource(form, canonical_resource)
            or code.consumed_at is not None
            or _expired(code.expires_at)
            or not _PKCE_VERIFIER.fullmatch(verifier)
            or not hmac.compare_digest(_s256(verifier), code.code_challenge)
        ):
            raise OAuthError("invalid_grant", "authorization code is invalid")
        if not self._repository.consume_code(digest=digest, consumed_at=now_iso()):
            raise OAuthError("invalid_grant", "authorization code is invalid")
        refresh_family_id = new_id(prefix="orf")
        minted = self._mint_access_token(
            project_id=code.project_id,
            owner_user_id=code.owner_user_id,
            parent_key_id=None,
            audience=code.resource,
            oauth_family_id=refresh_family_id,
        )
        return self._token_response(
            client=client,
            minted=minted,
            resource=code.resource,
            owner_user_id=code.owner_user_id,
            project_id=code.project_id,
            parent_refresh_token_id=None,
            refresh_family_id=refresh_family_id,
        )

    def refresh(
        self, *, form: dict[str, str], canonical_resource: str
    ) -> dict[str, Any]:
        client = self._token_client(form)
        if "refresh_token" not in client.grant_types:
            raise OAuthError("unauthorized_client", "client cannot use refresh_token")
        raw_token = _required_form(form, "refresh_token")
        digest = hash_secret(raw_token)
        token = self._repository.refresh_token_by_digest(digest=digest)
        if not secret_digest_matches(
            stored_digest=token.secret_digest if token else None,
            presented_digest=digest,
        ):
            raise OAuthError("invalid_grant", "refresh token is invalid")
        assert token is not None
        if token.consumed_at is not None:
            self._revoke_replayed_refresh(token)
            raise OAuthError("invalid_grant", "refresh token is invalid")
        if (
            token.client_id != client.client_id
            or token.resource != _required_resource(form, canonical_resource)
            or token.revoked_at is not None
            or _expired(token.expires_at)
        ):
            raise OAuthError("invalid_grant", "refresh token is invalid")
        if not self._repository.consume_refresh_token(
            token_id=token.id, consumed_at=now_iso()
        ):
            # We already established this token was unconsumed, unrevoked, and
            # unexpired above, so a failed compare-and-set means a concurrent
            # exchange consumed it first. That is refresh-token reuse: revoke
            # the whole family, exactly as the sequential-replay path does.
            self._revoke_replayed_refresh(token)
            raise OAuthError("invalid_grant", "refresh token is invalid")
        try:
            minted = self._project_keys.rotate(
                project_id=token.project_id,
                owner_user_id=token.owner_user_id,
                expires_at=iso_after(seconds=ACCESS_TOKEN_TTL_SECONDS),
                parent_key_id=token.current_key_id,
                sandbox_seconds_ceiling=None,
                blob_bytes_ceiling=None,
                audience=token.resource,
                oauth_family_id=token.family_id,
            )
        except NotFoundError as exc:
            raise OAuthError("invalid_grant", "refresh token is invalid") from exc
        return self._token_response(
            client=client,
            minted=minted,
            resource=token.resource,
            owner_user_id=token.owner_user_id,
            project_id=token.project_id,
            parent_refresh_token_id=token.id,
            refresh_family_id=token.family_id,
        )

    def _revoke_replayed_refresh(self, token: RefreshToken) -> None:
        self._repository.revoke_refresh_family_and_key_lineage(
            family_id=token.family_id,
            project_id=token.project_id,
            key_id=token.current_key_id,
            owner_user_id=token.owner_user_id,
            revoked_at=now_iso(),
        )

    def _authorization_request(
        self, *, params: dict[str, str], canonical_resource: str
    ) -> AuthorizationRequest:
        client_id = str(params.get("client_id") or "")
        client = self._repository.client_by_id(client_id=client_id)
        if client is None:
            raise OAuthError("invalid_request", "unknown client_id")
        redirect_uri = str(params.get("redirect_uri") or "")
        if not redirect_uri or redirect_uri not in client.redirect_uris:
            # An unregistered URI is never reflected into a redirect response.
            raise OAuthError("invalid_request", "redirect_uri is not registered")
        state = params.get("state")

        def redirectable(error: str, description: str) -> OAuthError:
            return OAuthError(
                error, description, redirect_uri=redirect_uri, state=state
            )

        if params.get("response_type") != "code":
            raise redirectable(
                "unsupported_response_type", "response_type must be code"
            )
        if params.get("code_challenge_method") != "S256":
            raise redirectable("invalid_request", "code_challenge_method must be S256")
        challenge = str(params.get("code_challenge") or "")
        if not _PKCE_CHALLENGE.fullmatch(challenge):
            raise redirectable("invalid_request", "code_challenge is invalid")
        if str(params.get("scope") or "").strip():
            raise redirectable("invalid_scope", "scopes are not supported")
        if str(params.get("response_mode") or "query") != "query":
            raise redirectable(
                "invalid_request", "only query response mode is supported"
            )
        try:
            resource = _required_resource(params, canonical_resource)
        except OAuthError as exc:
            raise redirectable(exc.error, exc.description) from exc
        return AuthorizationRequest(
            client=client,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
            resource=resource,
        )

    def _token_client(self, form: dict[str, str]) -> OAuthClient:
        client_id = _required_form(form, "client_id")
        client = self._repository.client_by_id(client_id=client_id)
        if client is None:
            raise OAuthError("invalid_client", "unknown public client")
        return client

    def _mint_access_token(
        self,
        *,
        project_id: str,
        owner_user_id: str,
        parent_key_id: str | None,
        audience: str,
        oauth_family_id: str,
    ) -> dict[str, Any]:
        return self._project_keys.create(
            project_id=project_id,
            owner_user_id=owner_user_id,
            expires_at=iso_after(seconds=ACCESS_TOKEN_TTL_SECONDS),
            parent_key_id=parent_key_id,
            sandbox_seconds_ceiling=None,
            blob_bytes_ceiling=None,
            audience=audience,
            oauth_family_id=oauth_family_id,
        )

    def _token_response(
        self,
        *,
        client: OAuthClient,
        minted: dict[str, Any],
        resource: str,
        owner_user_id: str,
        project_id: str,
        parent_refresh_token_id: str | None,
        refresh_family_id: str | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "access_token": str(minted["secret"]),
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        }
        if "refresh_token" not in client.grant_types:
            return response
        raw_refresh = mint_secret(prefix="mrt_", nbytes=32)
        key = dict(minted["key"])
        token = RefreshToken(
            id=new_id(prefix="ort"),
            family_id=refresh_family_id or new_id(prefix="orf"),
            secret_digest=hash_secret(raw_refresh),
            client_id=client.client_id,
            owner_user_id=owner_user_id,
            project_id=project_id,
            resource=resource,
            current_key_id=str(key["id"]),
            parent_token_id=parent_refresh_token_id,
            created_at=now_iso(),
            expires_at=iso_after(seconds=REFRESH_TOKEN_TTL_SECONDS),
            consumed_at=None,
            revoked_at=None,
        )
        try:
            self._repository.insert_refresh_token(token=token)
        except Exception:
            # Do not leave an untracked bearer active if refresh persistence fails.
            self._project_keys.revoke(
                project_id=project_id,
                key_id=str(key["id"]),
                owner_user_id=owner_user_id,
            )
            raise
        response["refresh_token"] = raw_refresh
        return response


def valid_redirect_uri(uri: str) -> bool:
    if (
        not uri
        or uri != uri.strip()
        or len(uri) > 2048
        or "\\" in uri
        or _has_control_character(uri)
    ):
        return False
    try:
        parsed = urlsplit(uri)
        # Accessing port validates malformed/out-of-range port text.
        parsed.port
    except ValueError:
        return False
    if (
        parsed.fragment
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname == "localhost"


def authorization_redirect(
    *,
    redirect_uri: str,
    issuer: str,
    state: str | None,
    code: str | None = None,
    error: str | None = None,
) -> str:
    parsed = urlsplit(redirect_uri)
    query = parsed.query
    fields = [("code", code)] if code else [("error", str(error or "server_error"))]
    if state is not None:
        fields.append(("state", state))
    fields.append(("iss", issuer))
    encoded = urlencode(fields)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            f"{query}&{encoded}" if query else encoded,
            "",
        )
    )


def oauth_error_redirect(*, exc: OAuthError, issuer: str) -> str | None:
    if exc.redirect_uri is None:
        return None
    parsed = urlsplit(exc.redirect_uri)
    query = parsed.query
    fields: list[tuple[str, str]] = [("error", exc.error)]
    if exc.state is not None:
        fields.append(("state", exc.state))
    fields.append(("iss", issuer))
    encoded = urlencode(fields)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            f"{query}&{encoded}" if query else encoded,
            "",
        )
    )


def _string_list(value: Any, *, field: str, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        raise OAuthError("invalid_client_metadata", f"{field} must be a nonempty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise OAuthError("invalid_client_metadata", f"{field} must contain strings")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise OAuthError(
            "invalid_client_metadata", f"{field} must not contain duplicates"
        )
    return result


def _required_form(form: dict[str, str], field: str) -> str:
    value = str(form.get(field) or "")
    if not value:
        raise OAuthError("invalid_request", f"{field} is required")
    return value


def _required_resource(values: dict[str, str], canonical_resource: str) -> str:
    resource = str(values.get("resource") or "")
    if resource != canonical_resource:
        raise OAuthError("invalid_target", "resource must identify this MCP endpoint")
    return resource


def _expired(value: str) -> bool:
    expiry = parse_iso(value)
    return expiry is None or expiry <= datetime.now(UTC)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


__all__ = [
    "ACCESS_TOKEN_TTL_SECONDS",
    "AUTHORIZATION_CODE_TTL_SECONDS",
    "OAuthControl",
    "OAuthError",
    "OAuthService",
    "REFRESH_TOKEN_TTL_SECONDS",
    "authorization_redirect",
    "oauth_error_redirect",
    "valid_redirect_uri",
]
