"""Request principal vocabulary.

The current private hosted-control deployment authenticates via Supabase JWTs,
RapidReview ``rr_sk_`` keys, and project-scoped ``mk_`` keys. HTTP requests run
as ``LOCAL_PRINCIPAL`` — the trusted-local sentinel — until a verifier resolves
a credential. A project (``mk_``) key carries its immutable project scope on the
principal; everything else (JWT, rr_sk_) carries none.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..kernel.identity import LOCAL_CLIENT_ID, LOCAL_TENANT_ID
from ..kernel.utils import PermissionDeniedError


class ProjectKeyScopeError(PermissionDeniedError):
    """A project key was presented outside its immutable project scope."""

    error_code = "project_scope_forbidden"


class ToolVisibilityError(PermissionDeniedError):
    """A non-local MCP caller attempted an internal-only tool."""

    error_code = "tool_visibility_forbidden"


class HumanSessionRequiredError(PermissionDeniedError):
    """A machine credential attempted a human-owned control operation."""

    error_code = "human_session_required"


@dataclass(frozen=True, slots=True)
class ProjectKeyQuotaContext:
    """The key's stored (not yet enforced) ceilings, for later phases."""

    key_id: str
    tenant_id: str
    sandbox_seconds_ceiling: int | None
    blob_bytes_ceiling: int | None


@dataclass(frozen=True)
class Principal:
    """The authenticated identity behind a request.

    ``tenant_id`` scopes every project-level record; ``client_id`` identifies
    the calling machine/daemon within the tenant (lease holder identity, audit
    attribution). ``user_id`` is the Supabase ``auth.users`` UUID when the
    request carried a verified credential — empty on the local surface, where
    project-membership filtering stays inactive. Local mode uses
    ``LOCAL_PRINCIPAL``.

    A project (``mk_``) key additionally binds an immutable ``key_project_id``
    and (for later OAuth + quota phases) ``audience``/``oauth_family_id`` plus
    stored ceilings; only such a principal carries a non-None ``key_id``.
    """

    tenant_id: str
    client_id: str
    user_id: str = ""
    key_id: str | None = None
    key_project_id: str | None = None
    audience: str | None = None
    oauth_family_id: str | None = None
    key_sandbox_seconds_ceiling: int | None = None
    key_blob_bytes_ceiling: int | None = None

    def key_quota_context(self) -> ProjectKeyQuotaContext | None:
        if self.key_id is None:
            return None
        return ProjectKeyQuotaContext(
            key_id=self.key_id,
            tenant_id=self.tenant_id,
            sandbox_seconds_ceiling=self.key_sandbox_seconds_ceiling,
            blob_bytes_ceiling=self.key_blob_bytes_ceiling,
        )


LOCAL_PRINCIPAL = Principal(tenant_id=LOCAL_TENANT_ID, client_id=LOCAL_CLIENT_ID)


def is_external_key(principal: object | None) -> bool:
    """Whether this principal is an external project (``mk_``) key."""
    return getattr(principal, "key_id", None) is not None


def is_local_principal(principal: object | None) -> bool:
    """Whether this is the trusted-local sentinel (internal composition).

    Only ``LOCAL_PRINCIPAL`` is trusted-local; every verifier-minted principal
    (JWT, rr_sk_, mk_) is external. The value-level check keeps the answer
    stable if the sentinel is ever reconstructed rather than shared by identity.
    """
    if principal is LOCAL_PRINCIPAL:
        return True
    return (
        getattr(principal, "key_id", None) is None
        and str(getattr(principal, "client_id", "")) == LOCAL_CLIENT_ID
        and not getattr(principal, "user_id", "")
    )


__all__ = [
    "HumanSessionRequiredError",
    "LOCAL_PRINCIPAL",
    "Principal",
    "ProjectKeyQuotaContext",
    "ProjectKeyScopeError",
    "ToolVisibilityError",
    "is_external_key",
    "is_local_principal",
]
