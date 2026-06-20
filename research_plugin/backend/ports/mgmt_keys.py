"""Port for per-sandbox management key custody."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class MgmtKeyStore(Protocol):
    """Control-plane custody of per-sandbox management keypairs."""

    def ensure(self, *, experiment_id: str) -> str:
        """Mint or reuse a keypair and return the public key."""
        ...

    def key_path(self, *, experiment_id: str) -> Path:
        """Return the private-key path for the management SSH channel."""
        ...

    def remove(self, *, experiment_id: str) -> None:
        """Drop the keypair."""
        ...
