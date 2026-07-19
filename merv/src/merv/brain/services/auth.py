"""Supabase-backed request authentication (hosted control mode only).

The research suite shares RapidReview's Supabase project: the same accounts
sign in to both products. One ``Authorization: Bearer`` header carries either
a Supabase session JWT (verified locally, HS256) or a long-lived RapidReview
``rr_sk_`` API key (sha256 hash looked up in the shared ``api_keys`` table
over PostgREST). Local mode never constructs a verifier, so none of this —
including the PyJWT import — executes on the localhost path.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from ..research_core.domain.vocabulary import LOCAL_TENANT_ID
from .identity import Principal

SUPABASE_URL_ENV_VAR = "SUPABASE_URL"
SUPABASE_JWT_SECRET_ENV_VAR = "SUPABASE_JWT_SECRET"
# Same value RapidReview calls SUPABASE_KEY (service role — bypasses RLS so
# the api_keys hash lookup works). Server-side only; never reaches clients.
SUPABASE_SERVICE_KEY_ENV_VAR = "SUPABASE_SERVICE_KEY"
SUPABASE_ANON_KEY_ENV_VAR = "SUPABASE_ANON_KEY"

API_KEY_PREFIX = "rr_sk_"
_KEY_CACHE_TTL_SECONDS = 60.0


class UnauthorizedError(Exception):
    """Credential missing, malformed, expired, or unknown."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class SupabaseVerifier:
    """Verifies Supabase JWTs and RapidReview API keys into Principals."""

    supabase_url: str
    jwt_secret: str
    service_key: str = ""
    anon_key: str = ""
    _key_cache: dict[str, tuple[str, float]] = field(default_factory=dict)
    _http: httpx.Client | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SupabaseVerifier | None":
        source = env if env is not None else os.environ
        url = (source.get(SUPABASE_URL_ENV_VAR) or "").strip().rstrip("/")
        secret = (source.get(SUPABASE_JWT_SECRET_ENV_VAR) or "").strip()
        if not url or not secret:
            return None
        return cls(
            supabase_url=url,
            jwt_secret=secret,
            service_key=(source.get(SUPABASE_SERVICE_KEY_ENV_VAR) or "").strip(),
            anon_key=(source.get(SUPABASE_ANON_KEY_ENV_VAR) or "").strip(),
        )

    def meta(self) -> dict[str, object]:
        """The /api/meta auth block: public values only, never the secrets."""
        return {
            "required": True,
            "supabase_url": self.supabase_url,
            "supabase_anon_key": self.anon_key,
        }

    def verify_bearer(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise UnauthorizedError("missing bearer credential")
        token = authorization[len("Bearer "):].strip()
        if not token:
            raise UnauthorizedError("empty bearer credential")
        if token.startswith(API_KEY_PREFIX):
            return self._verify_api_key(token)
        return self._verify_jwt(token)

    def verify_basic_or_bearer(self, authorization: str | None) -> Principal:
        """Bearer plus HTTP Basic (password slot carries the credential).

        Basic exists for the MLflow gate: browsers answer its 401 challenge
        with a native prompt, and the MLflow client emits Basic for
        MLFLOW_TRACKING_USERNAME/PASSWORD pairs.
        """
        if authorization and authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[len("Basic "):]).decode("utf-8")
                _, _, password = decoded.partition(":")
            except Exception as exc:
                raise UnauthorizedError("malformed basic credential") from exc
            return self.verify_bearer(f"Bearer {password.strip()}")
        return self.verify_bearer(authorization)

    def _verify_jwt(self, token: str) -> Principal:
        # Lazy import: PyJWT ships in the `control` extra; the local preset
        # imports this module (via composition) but must never need it.
        import jwt

        try:
            payload = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
        except jwt.ExpiredSignatureError as exc:
            raise UnauthorizedError("token expired") from exc
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("invalid token") from exc
        if payload.get("is_anonymous"):
            raise UnauthorizedError("anonymous sessions are not accepted")
        sub = str(payload.get("sub") or "")
        if not sub:
            raise UnauthorizedError("token has no subject")
        session = str(payload.get("session_id") or sub[:8])
        return Principal(
            tenant_id=LOCAL_TENANT_ID, client_id=f"jwt:{session}", user_id=sub
        )

    def _verify_api_key(self, key: str) -> Principal:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        cached = self._key_cache.get(digest)
        if cached and cached[1] > time.monotonic():
            user_id = cached[0]
        else:
            user_id = self._lookup_key_user(digest)
            self._key_cache[digest] = (user_id, time.monotonic() + _KEY_CACHE_TTL_SECONDS)
        return Principal(
            tenant_id=LOCAL_TENANT_ID, client_id=f"key:{digest[:8]}", user_id=user_id
        )

    def _lookup_key_user(self, digest: str) -> str:
        if not self.service_key:
            raise UnauthorizedError("API keys are not enabled on this deployment")
        try:
            response = self._client().get(
                f"{self.supabase_url}/rest/v1/api_keys",
                params={"key_hash": f"eq.{digest}", "select": "user_id", "limit": "1"},
                headers={
                    "apikey": self.service_key,
                    "Authorization": f"Bearer {self.service_key}",
                },
            )
            response.raise_for_status()
            rows = response.json()
        except UnauthorizedError:
            raise
        except Exception as exc:
            raise UnauthorizedError("credential service unavailable") from exc
        if not rows:
            raise UnauthorizedError("unknown API key")
        return str(rows[0]["user_id"])

    def refresh_session(self, refresh_token: str) -> dict[str, object]:
        """Exchange a refresh token for a fresh session via Supabase Auth REST.

        Proxied by the brain (public anon key) so CLI clients never talk to
        Supabase directly — the same contract the RapidReview backend exposes.
        """
        if not self.anon_key:
            raise UnauthorizedError("session refresh is not enabled on this deployment")
        try:
            response = self._client().post(
                f"{self.supabase_url}/auth/v1/token",
                params={"grant_type": "refresh_token"},
                json={"refresh_token": refresh_token},
                headers={"apikey": self.anon_key},
            )
        except Exception as exc:
            raise UnauthorizedError("credential service unavailable") from exc
        if response.status_code != 200:
            raise UnauthorizedError("refresh token rejected")
        payload = response.json()
        return {
            "access_token": str(payload.get("access_token") or ""),
            "refresh_token": str(payload.get("refresh_token") or refresh_token),
            "expires_in": int(payload.get("expires_in") or 3600),
        }

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=5.0)
        return self._http
