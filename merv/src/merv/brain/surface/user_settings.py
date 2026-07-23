"""Write-only per-user secret settings (no-dataplane Phase C).

Currently just the per-user Hugging Face token: a member brings their own so no
deployment-wide HF secret exists. The value is WRITE-ONLY — set or cleared here,
never returned by any read — and is consumed only internally at sandbox
provisioning (see ``SandboxFacade._resolve_hf_token``). The persistence lives on
the kernel record store (``user_hf_tokens``, KERNEL-owned); this surface facade
is the write path the REST route and composition depend on.
"""

from __future__ import annotations

from ..kernel.state.store import BaseStateStore
from ..kernel.utils import ValidationError

# A generous cap so a fat-fingered paste (or hostile body) cannot store an
# unbounded blob; real HF tokens are well under this.
_MAX_HF_TOKEN_CHARS = 500


class UserHfTokenSettings:
    """Set/clear a user's Hugging Face token; the value never reads back."""

    def __init__(self, *, store: BaseStateStore) -> None:
        self._store = store

    def set_token(self, *, user_id: str, token: str) -> dict[str, object]:
        token = (token or "").strip()
        if not token:
            raise ValidationError("token is required")
        if len(token) > _MAX_HF_TOKEN_CHARS:
            raise ValidationError(
                f"token is too long (max {_MAX_HF_TOKEN_CHARS} characters)"
            )
        self._store.set_user_hf_token(user_id=user_id, token=token)
        return {"status": "set"}

    def clear_token(self, *, user_id: str) -> dict[str, object]:
        self._store.clear_user_hf_token(user_id=user_id)
        return {"status": "cleared"}


__all__ = ["UserHfTokenSettings"]
