"""S3-backed content-addressed blob store (cloud plan Phase 8, decision 7).

The cloud implementation of the same ``BlobStore`` protocol as
``LocalDirBlobStore``, behind the same contract tests (``BlobStoreContractMixin``
against a dockerized minio). ``S3BlobStore.presign_put`` returns a real
single-use HTTPS PUT URL for off-process producers.

boto3 is imported lazily (gated): the daemon/proxy/local profiles never need it,
so installing the package is a control-profile concern only.

Layout, keyed ``tenant/sha256`` like the local store:
- ``<namespace>/<sha256>`` — the content-addressed object.
- ``.uploads/<upload_id>`` — a staging key a presigned PUT lands in;
  ``finalize_put`` hashes it, copies to the content key, and deletes staging.
TTL: per-object ``expires_at`` lives in object metadata; ``sweep_expired`` lists
and deletes past-due objects (S3 lifecycle rules are the Phase 9 productionized
backstop, but the protocol method is implemented here so the contract holds).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..utils import NotFoundError, ValidationError, new_id, now_iso
from .blobs import BlobStat, _validate_keys


_UPLOAD_PREFIX = ".uploads/"


class S3BlobStore:
    """BlobStore over an S3-compatible bucket (boto3; gated import)."""

    def __init__(
        self,
        *,
        bucket: str,
        client: Any | None = None,
        presign_expiry_seconds: int = 3600,
    ) -> None:
        self.bucket = bucket
        self.presign_expiry_seconds = presign_expiry_seconds
        if client is not None:
            self._s3 = client
        else:
            import boto3  # gated: control profile only

            self._s3 = boto3.client("s3")

    # ---- content-addressed put/get/stat/delete ----

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
        key = self._key(namespace=namespace, sha256=sha)
        existing = self._head(key=key)
        if existing is not None:
            # Idempotent: only ever EXTEND expiry (never shorten), matching the
            # local store's contract.
            self._maybe_extend_expiry(key=key, head=existing, expires_at=expires_at)
            return sha
        meta = {"sha256": sha, "namespace": namespace}
        if expires_at:
            meta["expires_at"] = expires_at
        self._s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata=meta,
        )
        return sha

    def get(self, *, namespace: str, sha256: str) -> bytes:
        _validate_keys(namespace=namespace, sha256=sha256)
        key = self._key(namespace=namespace, sha256=sha256)
        try:
            obj = self._s3.get_object(Bucket=self.bucket, Key=key)
        except self._s3.exceptions.NoSuchKey as exc:
            raise NotFoundError(f"blob not found: {namespace}/{sha256}") from exc
        except Exception as exc:  # noqa: BLE001 — map a 404 ClientError too
            if _is_not_found(exc):
                raise NotFoundError(f"blob not found: {namespace}/{sha256}") from exc
            raise
        return obj["Body"].read()

    def presign_get(self, *, namespace: str, sha256: str) -> dict[str, Any]:
        _validate_keys(namespace=namespace, sha256=sha256)
        key = self._key(namespace=namespace, sha256=sha256)
        if self._head(key=key) is None:
            raise NotFoundError(f"blob not found: {namespace}/{sha256}")
        return {
            "url": self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.presign_expiry_seconds,
            )
        }

    def stat(self, *, namespace: str, sha256: str) -> BlobStat | None:
        _validate_keys(namespace=namespace, sha256=sha256)
        head = self._head(key=self._key(namespace=namespace, sha256=sha256))
        if head is None:
            return None
        meta = head.get("Metadata") or {}
        return BlobStat(
            sha256=sha256,
            namespace=namespace,
            size_bytes=int(head.get("ContentLength") or 0),
            content_type=str(head.get("ContentType") or "application/octet-stream"),
            created_at=str(meta.get("created_at") or now_iso()),
            expires_at=meta.get("expires_at"),
        )

    def delete(self, *, namespace: str, sha256: str) -> bool:
        _validate_keys(namespace=namespace, sha256=sha256)
        key = self._key(namespace=namespace, sha256=sha256)
        existed = self._head(key=key) is not None
        self._s3.delete_object(Bucket=self.bucket, Key=key)
        return existed

    # ---- single-use presigned uploads (real HTTPS) ----

    def presign_put(
        self,
        *,
        namespace: str,
        max_size_bytes: int,
        expires_at: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        _validate_keys(namespace=namespace)
        upload_id = new_id(prefix="upload")
        staging_key = f"{_UPLOAD_PREFIX}{upload_id}"
        # Stash the finalize parameters in a sidecar so finalize_put is
        # stateless against the store (the local impl uses a meta file).
        sidecar = {
            "upload_id": upload_id,
            "namespace": namespace,
            "max_size_bytes": int(max_size_bytes),
            "content_type": content_type,
            "expires_at": expires_at,
            "staging_key": staging_key,
        }
        self._s3.put_object(
            Bucket=self.bucket,
            Key=f"{staging_key}.meta",
            Body=json.dumps(sidecar, sort_keys=True).encode("utf-8"),
        )
        url = self._s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": staging_key, "ContentType": content_type},
            ExpiresIn=self.presign_expiry_seconds,
        )
        return {
            "upload_id": upload_id,
            "url": url,
            "max_size_bytes": int(max_size_bytes),
            "expires_at": expires_at,
        }

    def finalize_put(self, *, upload_id: str) -> BlobStat:
        meta_key = f"{_UPLOAD_PREFIX}{upload_id}.meta"
        try:
            meta_obj = self._s3.get_object(Bucket=self.bucket, Key=meta_key)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise NotFoundError(
                    f"unknown or already-consumed upload: {upload_id}"
                ) from exc
            raise
        sidecar = json.loads(meta_obj["Body"].read().decode("utf-8"))
        staging_key = str(sidecar["staging_key"])
        try:
            if self._head(key=staging_key) is None:
                raise NotFoundError(f"upload received no bytes: {upload_id}")
            data = self._s3.get_object(Bucket=self.bucket, Key=staging_key)["Body"].read()
            max_size = int(sidecar["max_size_bytes"])
            if len(data) > max_size:
                raise ValidationError(
                    f"upload {upload_id} exceeds its size cap: "
                    f"{len(data)} > {max_size} bytes"
                )
            sha = self.put(
                namespace=str(sidecar["namespace"]),
                data=data,
                content_type=str(sidecar["content_type"]),
                expires_at=sidecar.get("expires_at"),
            )
        finally:
            # Single use either way: drop the staging object + sidecar.
            self._s3.delete_object(Bucket=self.bucket, Key=staging_key)
            self._s3.delete_object(Bucket=self.bucket, Key=meta_key)
        stat = self.stat(namespace=str(sidecar["namespace"]), sha256=sha)
        assert stat is not None
        return stat

    def sweep_expired(self, *, now: str | None = None) -> int:
        cutoff = now or now_iso()
        swept = 0
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            for item in page.get("Contents", []) or []:
                key = str(item["Key"])
                if key.startswith(_UPLOAD_PREFIX):
                    continue
                head = self._head(key=key)
                if head is None:
                    continue
                expires_at = (head.get("Metadata") or {}).get("expires_at")
                if not expires_at or str(expires_at) > cutoff:
                    continue
                self._s3.delete_object(Bucket=self.bucket, Key=key)
                swept += 1
        return swept

    # ---- helpers ----

    def _key(self, *, namespace: str, sha256: str) -> str:
        return f"{namespace}/{sha256}"

    def _head(self, *, key: str) -> dict[str, Any] | None:
        try:
            return self._s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise

    def _maybe_extend_expiry(
        self, *, key: str, head: dict[str, Any], expires_at: str | None
    ) -> None:
        meta = dict(head.get("Metadata") or {})
        current = meta.get("expires_at")
        if current is None:
            return  # already pinned forever
        if expires_at is None or str(expires_at) > str(current):
            new_meta = dict(meta)
            if expires_at is None:
                new_meta.pop("expires_at", None)
            else:
                new_meta["expires_at"] = expires_at
            # Copy onto itself to replace metadata (S3 metadata is immutable
            # without a copy).
            self._s3.copy_object(
                Bucket=self.bucket,
                Key=key,
                CopySource={"Bucket": self.bucket, "Key": key},
                Metadata=new_meta,
                MetadataDirective="REPLACE",
            )


def _is_not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
    return code in {"404", "NoSuchKey", "NotFound"} or exc.__class__.__name__ in {
        "NoSuchKey",
        "404",
    }
