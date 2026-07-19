"""Shared helpers for high-entropy opaque bearer secrets."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def mint_secret(*, prefix: str, nbytes: int) -> str:
    """Mint a URL-safe high-entropy secret with the caller's public prefix."""
    return f"{prefix}{secrets.token_urlsafe(nbytes)}"


def hash_secret(secret: str) -> str:
    """Stored form for high-entropy opaque secrets: sha256 hex digest."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secret_digest_matches(*, stored_digest: object | None, presented_digest: str) -> bool:
    """Constant-time comparison for a stored digest and a presented digest.

    ``None`` burns the same compare primitive and returns false, which keeps
    unknown-token paths from growing a separate early-return compare shape.
    """
    if stored_digest is None:
        hmac.compare_digest(presented_digest, presented_digest)
        return False
    return hmac.compare_digest(str(stored_digest), presented_digest)
