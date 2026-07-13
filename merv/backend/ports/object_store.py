"""Provider port for heavy, content-addressed object storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ObjectStat:
    sha256: str
    namespace: str
    size_bytes: int
    content_type: str
    created_at: str


class ObjectStore(Protocol):
    """Heavy object storage: producers move bytes; control mints URLs and verifies."""

    def presign_upload(
        self,
        *,
        namespace: str,
        sha256: str,
        size_bytes: int,
        content_type: str = "application/octet-stream",
        expires_in: int,
    ) -> dict[str, Any]:
        ...

    def complete_upload(
        self, *, upload_id: str, parts: list[dict[str, Any]] | None = None
    ) -> ObjectStat:
        ...

    def abort_upload(self, *, upload_id: str) -> bool: ...

    def presign_download(
        self, *, namespace: str, sha256: str, expires_in: int
    ) -> dict[str, Any]:
        ...

    def stat(self, *, namespace: str, sha256: str) -> ObjectStat | None: ...

    def delete(self, *, namespace: str, sha256: str) -> bool: ...


__all__ = ["ObjectStat", "ObjectStore"]
