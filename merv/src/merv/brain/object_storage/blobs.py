"""Content-addressed blob storage for gated artifacts and generated objects.

The storage model uses one sha256-keyed, namespace-scoped store shared by
artifact submissions (gated-role bytes pinned at artifact.submit), report
figures, and metrics snapshots.
The local implementation is a directory under the brain state root; hosted
control uses the S3-compatible implementation behind the same protocol.

Blobs are never deduplicated across namespaces because cross-project namespace
deduplication would leak content existence.

Single-use uploads: ``presign_put`` mints an upload target for bytes produced
off-process and ``finalize_put`` lands them content-addressed, enforcing the
size cap and single use. The local implementation's "URL" is a ``file://`` path
— honest to the seam, not to the transport: real presigned HTTPS URLs arrive
with ``S3BlobStore``.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from pathlib import Path

from ..kernel.ports.blob_store import (
    BlobDownloadTarget,
    BlobStat,
    BlobStore,
    BlobUploadTarget,
    validate_blob_keys,
)
from ..kernel.utils import NotFoundError, ValidationError, new_id, now_iso

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
        validate_blob_keys(namespace=namespace)
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
        validate_blob_keys(namespace=namespace, sha256=sha256)
        blob_path = self._blob_path(namespace=namespace, sha256=sha256)
        if not blob_path.exists():
            raise NotFoundError(f"blob not found: {namespace}/{sha256}")
        return blob_path.read_bytes()

    def presign_get(
        self, *, namespace: str, sha256: str
    ) -> BlobDownloadTarget:
        validate_blob_keys(namespace=namespace, sha256=sha256)
        blob_path = self._blob_path(namespace=namespace, sha256=sha256)
        if not blob_path.exists():
            raise NotFoundError(f"blob not found: {namespace}/{sha256}")
        return {"url": blob_path.resolve().as_uri()}

    def stat(self, *, namespace: str, sha256: str) -> BlobStat | None:
        validate_blob_keys(namespace=namespace, sha256=sha256)
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
        validate_blob_keys(namespace=namespace, sha256=sha256)
        blob_path = self._blob_path(namespace=namespace, sha256=sha256)
        meta_path = self._meta_path(namespace=namespace, sha256=sha256)
        existed = blob_path.exists()
        for path in (blob_path, meta_path):
            with suppress(FileNotFoundError):
                path.unlink()
        return existed

    def presign_put(
        self,
        *,
        namespace: str,
        max_size_bytes: int,
        expires_at: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> BlobUploadTarget:
        """Single-use upload target backed by a local staging file.

        The returned ``url`` is a ``file://`` path the producer can write
        with ``curl -T`` (or a plain file write) — an honest local stand-in
        for the seam, not for the transport: a sandbox VM cannot reach this
        path, which is exactly why ``S3BlobStore`` (Phase 8) must return a
        real single-use HTTPS PUT URL behind these same verbs. The contract
        bites in ``finalize_put``: size cap, single use, content addressing.
        """
        validate_blob_keys(namespace=namespace)
        upload_id = new_id(prefix="upload")
        staging = self._staging_path(upload_id=upload_id)
        staging.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "upload_id": upload_id,
            "namespace": namespace,
            "max_size_bytes": int(max_size_bytes),
            "content_type": content_type,
            "expires_at": expires_at,
            "created_at": now_iso(),
        }
        self._staging_meta_path(upload_id=upload_id).write_text(
            json.dumps(meta, sort_keys=True), encoding="utf-8"
        )
        return {
            "upload_id": upload_id,
            "url": staging.resolve().as_uri(),
            "max_size_bytes": int(max_size_bytes),
            "expires_at": expires_at,
        }

    def finalize_put(self, *, upload_id: str) -> BlobStat:
        staging = self._staging_path(upload_id=upload_id)
        meta_path = self._staging_meta_path(upload_id=upload_id)
        if not meta_path.exists():
            raise NotFoundError(f"unknown or already-consumed upload: {upload_id}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        try:
            if not staging.exists():
                raise NotFoundError(f"upload received no bytes: {upload_id}")
            data = staging.read_bytes()
            max_size_bytes = int(meta["max_size_bytes"])
            if len(data) > max_size_bytes:
                raise ValidationError(
                    f"upload {upload_id} exceeds its size cap: "
                    f"{len(data)} > {max_size_bytes} bytes"
                )
            sha = self.put(
                namespace=str(meta["namespace"]),
                data=data,
                content_type=str(meta["content_type"]),
                expires_at=meta.get("expires_at"),
            )
        finally:
            # Single use either way: a failed finalize consumes the target.
            for path in (staging, meta_path):
                with suppress(FileNotFoundError):
                    path.unlink()
        stat = self.stat(namespace=str(meta["namespace"]), sha256=sha)
        assert stat is not None
        return stat

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

    # Staging lives one level deep (".uploads/<id>"), so the expiry sweep's
    # three-level blob glob never sees it; namespaces are project ids in
    # practice and never collide with the dot-name.
    def _staging_path(self, *, upload_id: str) -> Path:
        return self.root / ".uploads" / upload_id

    def _staging_meta_path(self, *, upload_id: str) -> Path:
        return self.root / ".uploads" / f"{upload_id}.meta.json"

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


__all__ = [
    "BlobDownloadTarget",
    "BlobStat",
    "BlobStore",
    "BlobUploadTarget",
    "LocalDirBlobStore",
]
