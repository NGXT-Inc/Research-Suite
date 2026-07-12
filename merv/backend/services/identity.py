"""Request principal vocabulary.

The current private hosted-control deployment does not have user auth yet. HTTP
requests run as the implicit local principal until the real auth system replaces
this placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.vocabulary import LOCAL_CLIENT_ID, LOCAL_TENANT_ID


@dataclass(frozen=True)
class Principal:
    """The authenticated identity behind a request.

    ``tenant_id`` scopes every project-level record; ``client_id`` identifies
    the calling machine/daemon within the tenant (lease holder identity, audit
    attribution). ``user_id`` is the Supabase ``auth.users`` UUID when the
    request carried a verified credential — empty on the local surface, where
    project-membership filtering stays inactive. Local mode uses
    ``LOCAL_PRINCIPAL``.
    """

    tenant_id: str
    client_id: str
    user_id: str = ""


LOCAL_PRINCIPAL = Principal(tenant_id=LOCAL_TENANT_ID, client_id=LOCAL_CLIENT_ID)
