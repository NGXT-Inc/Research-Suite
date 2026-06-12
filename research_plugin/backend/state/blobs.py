"""Content-addressed blob storage for gated artifacts and recovery objects.

Decision 7 of docs/CLOUD_BACKEND_MIGRATION_PLAN.md: one sha256-keyed,
namespace-scoped store shared by artifact submissions (gated-role bytes
captured at resource.associate), report figures, metrics snapshots, and the
expiry parachute. The local implementation is a plain directory under
``.research_plugin/blobs/``; the cloud implementation (S3, Phase 8) implements
the same protocol behind the same contract tests.

The namespace maps to the project locally and to ``tenant/project`` in the
cloud — blobs are never deduplicated across namespaces (cross-tenant dedup
would leak content existence).

Presigned uploads (the figure tier and the parachute PUT) join the protocol in
Phase 8, when an HTTP surface exists to accept them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..utils import NotFoundError, ValidationError, now_iso


_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BlobStat:
    sha256: str
    namespace: str
    size_bytes: int
    content_type: str
    created_at: str
    expires_at: str | None


class BlobStore(Protocol):
    """Content-addressed, namespace-scoped byte storage."""

    def put(
        self,
        *,
        namespace: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        expires_at: str | None = None,
    ) -> str:
        """Store ``data``; returns its sha256 hex key. Idempotent: re-putting
        identical bytes is a no-op (an existing blob's expiry is only ever
        extended, never shortened)."""
        ...

    def get(self, *, namespace: str, sha256: str) -> bytes:
        """Return the blob's bytes; raises NotFoundError when absent."""
        ...

    def stat(self, *, namespace: str, sha256: str) -> BlobStat | None: ...

    def delete(self, *, namespace: str, sha256: str) -> bool: ...

    def sweep_expired(self, *, now: str | None = None) -> int:
        """Delete blobs whose ``expires_at`` is past ``now``; returns count."""
        ...


def _validate_keys(*, namespace: str, sha256: str | None = None) -> None:
    if not namespace or not _NAMESPACE_RE.match(namespace):
        raise ValidationError(f"invalid blob namespace: {namespace!r}")
    if sha256 is not None and not _SHA256_RE.match(sha256):
        raise ValidationError(f"invalid blob key (expected sha256 hex): {sha256!r}")


class LocalDirBlobStore:
    """Blob store rooted at a local directory (local-mode implementation).

    Layout: ``<root>/<namespace>/<sha[:2]>/<sha>`` with a ``<sha>.meta.json``
    sidecar carrying size/content_type/created_at/expires_at. Self-contained —
    no database coupling, so the store can be pointed at any directory.
    """

    def __init__(self, *, root: Path) -> None:
        self.root = root

    def put(
        self,
        *,
        namespace: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        expires_at: str | None = None,
    ) -> str:
        _validate_keys(namespace=namespace)
        sha = hashlib.sha256(data).hexdigest()
        blob_path = self._blob_path(namespace=namespace, sha256=sha)
        meta_path = self._meta_path(namespace=namespace, sha256=sha)
        if blob_path.exists():
            self._extend_expiry(meta_path=meta_path, expires_at=expires_at)
            return sha
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = blob_path.with_suffix(".tmp")
        tmp_path.write_bytes(data)
        os.replace(tmp_path, blob_path)
        meta = {
            "sha256": sha,
            "namespace": namespace,
            "size_bytes": len(data),
            "content_type": content_type,
            "created_at": now_iso(),
            "expires_at": expires_at,
        }
        meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        return sha

    def get(self, *, namespace: str, sha256: str) -> bytes:
        _validate_keys(namespace=namespace, sha256=sha256)
        blob_path = self._blob_path(namespace=namespace, sha256=sha256)
        if not blob_path.exists():
            raise NotFoundError(f"blob not found: {namespace}/{sha256}")
        return blob_path.read_bytes()

    def stat(self, *, namespace: str, sha256: str) -> BlobStat | None:
        _validate_keys(namespace=namespace, sha256=sha256)
        meta_path = self._meta_path(namespace=namespace, sha256=sha256)
        blob_path = self._blob_path(namespace=namespace, sha256=sha256)
        if not blob_path.exists() or not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return BlobStat(
            sha256=str(meta["sha256"]),
            namespace=str(meta["namespace"]),
            size_bytes=int(meta["size_bytes"]),
            content_type=str(meta["content_type"]),
            created_at=str(meta["created_at"]),
            expires_at=meta.get("expires_at"),
        )

    def delete(self, *, namespace: str, sha256: str) -> bool:
        _validate_keys(namespace=namespace, sha256=sha256)
        blob_path = self._blob_path(namespace=namespace, sha256=sha256)
        meta_path = self._meta_path(namespace=namespace, sha256=sha256)
        existed = blob_path.exists()
        for path in (blob_path, meta_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return existed

    def sweep_expired(self, *, now: str | None = None) -> int:
        cutoff = now or now_iso()
        swept = 0
        if not self.root.exists():
            return 0
        for meta_path in self.root.glob("*/*/*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            expires_at = meta.get("expires_at")
            if not expires_at or str(expires_at) > cutoff:
                continue
            if self.delete(namespace=str(meta["namespace"]), sha256=str(meta["sha256"])):
                swept += 1
        return swept

    def _blob_path(self, *, namespace: str, sha256: str) -> Path:
        return self.root / namespace / sha256[:2] / sha256

    def _meta_path(self, *, namespace: str, sha256: str) -> Path:
        return self.root / namespace / sha256[:2] / f"{sha256}.meta.json"

    def _extend_expiry(self, *, meta_path: Path, expires_at: str | None) -> None:
        """An existing blob's lifetime only ever grows: a re-put with no expiry
        clears the deadline (pinned forever); a later expiry extends it; an
        earlier one is ignored."""
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        current = meta.get("expires_at")
        if current is None:
            return
        if expires_at is None or str(expires_at) > str(current):
            meta["expires_at"] = expires_at
            meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
