"""Port for heavy-object byte-budget admission."""

from __future__ import annotations

from typing import Any, Protocol


class BlobQuotaAdmission(Protocol):
    """Checks a byte reservation inside its caller-owned ledger transaction."""

    def check_reservation(
        self,
        *,
        conn: Any,
        project_id: str,
        sha256: str,
        size_bytes: int,
    ) -> None: ...


__all__ = ["BlobQuotaAdmission"]
