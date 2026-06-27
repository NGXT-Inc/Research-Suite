"""S3-compatible heavy-object store for R2, AWS S3, and MinIO."""

from __future__ import annotations

import base64
import json
import math
from typing import Any

from ..ports.object_store import ObjectStat
from ..state.blobs import _validate_keys
from ..utils import NotFoundError, ValidationError, new_id, now_iso


_UPLOAD_PREFIX = ".uploads/"
DEFAULT_MULTIPART_THRESHOLD_BYTES = 64 * 1024 * 1024
DEFAULT_MULTIPART_PART_BYTES = 64 * 1024 * 1024


class S3CompatibleObjectStore:
    """ObjectStore over an S3-compatible bucket (boto3; gated import)."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: Any | None = None,
        multipart_threshold_bytes: int = DEFAULT_MULTIPART_THRESHOLD_BYTES,
        multipart_part_bytes: int = DEFAULT_MULTIPART_PART_BYTES,
    ) -> None:
        self.bucket = bucket
        self.multipart_threshold_bytes = int(multipart_threshold_bytes)
        self.multipart_part_bytes = int(multipart_part_bytes)
        if client is not None:
            self._s3 = client
        else:
            import boto3  # gated: control profile only

            client_kwargs = {"endpoint_url": endpoint_url, "region_name": region_name}
            if access_key_id is not None and secret_access_key is not None:
                # Keep storage creds independent while preserving boto3 fallback.
                client_kwargs.update(
                    {
                        "aws_access_key_id": access_key_id,
                        "aws_secret_access_key": secret_access_key,
                    }
                )
            self._s3 = boto3.client("s3", **client_kwargs)

    def presign_upload(
        self,
        *,
        namespace: str,
        sha256: str,
        size_bytes: int,
        content_type: str = "application/octet-stream",
        expires_in: int,
    ) -> dict[str, Any]:
        _validate_keys(namespace=namespace, sha256=sha256)
        upload_id = new_id(prefix="upload")
        key = self._key(namespace=namespace, sha256=sha256)
        checksum = _sha256_b64(sha256)
        sidecar = {
            "upload_id": upload_id,
            "namespace": namespace,
            "sha256": sha256,
            "size_bytes": int(size_bytes),
            "content_type": content_type,
            "created_at": now_iso(),
        }
        if int(size_bytes) > self.multipart_threshold_bytes:
            s3_upload = self._s3.create_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                ContentType=content_type,
            )
            s3_upload_id = str(s3_upload["UploadId"])
            part_size = self._part_size(size_bytes=int(size_bytes))
            sidecar.update(
                {
                    "mode": "multipart",
                    "s3_upload_id": s3_upload_id,
                    "part_size": part_size,
                }
            )
            self._put_sidecar(upload_id=upload_id, sidecar=sidecar)
            part_count = max(1, math.ceil(int(size_bytes) / part_size))
            return {
                "upload_id": upload_id,
                "parts": [
                    {
                        "part_number": part_number,
                        "url": self._s3.generate_presigned_url(
                            "upload_part",
                            Params={
                                "Bucket": self.bucket,
                                "Key": key,
                                "UploadId": s3_upload_id,
                                "PartNumber": part_number,
                            },
                            ExpiresIn=int(expires_in),
                        ),
                    }
                    for part_number in range(1, part_count + 1)
                ],
                "part_size": part_size,
                "size_bytes": int(size_bytes),
                "content_type": content_type,
                "checksum_sha256": checksum,
            }
        sidecar["mode"] = "single"
        self._put_sidecar(upload_id=upload_id, sidecar=sidecar)
        return {
            "upload_id": upload_id,
            "url": self._s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ContentType": content_type,
                    "ChecksumSHA256": checksum,
                },
                ExpiresIn=int(expires_in),
            ),
            "size_bytes": int(size_bytes),
            "content_type": content_type,
            "checksum_sha256": checksum,
        }

    def complete_upload(
        self, *, upload_id: str, parts: list[dict[str, Any]] | None = None
    ) -> ObjectStat:
        sidecar = self._get_sidecar(upload_id=upload_id)
        key = self._key(namespace=str(sidecar["namespace"]), sha256=str(sidecar["sha256"]))
        try:
            if sidecar.get("mode") == "multipart":
                try:
                    self._complete_multipart(sidecar=sidecar, parts=parts)
                except Exception:
                    self._abort_multipart(sidecar=sidecar)
                    raise
            size_bytes = int(sidecar["size_bytes"])
            if sidecar.get("mode") == "multipart":
                # Multipart trusts producer-declared sha content keys; we size-verify and do not re-hash GB objects server-side.
                head = self._head(key=key)
                if head is None:
                    raise NotFoundError(f"upload received no bytes: {upload_id}")
                actual_size = int(head.get("ContentLength") or 0)
                if actual_size != size_bytes:
                    raise ValidationError(
                        f"upload {upload_id} size mismatch: "
                        f"{actual_size} != {size_bytes} bytes"
                    )
                stat = self.stat(
                    namespace=str(sidecar["namespace"]), sha256=str(sidecar["sha256"])
                )
                assert stat is not None
                return stat
            head = self._head(key=key, checksum=True)
            if head is None:
                raise NotFoundError(f"upload received no bytes: {upload_id}")
            actual_size = int(head.get("ContentLength") or 0)
            if actual_size > size_bytes:
                raise ValidationError(
                    f"upload {upload_id} exceeds its size cap: "
                    f"{actual_size} > {size_bytes} bytes"
                )
            checksum = head.get("ChecksumSHA256")
            if checksum != _sha256_b64(str(sidecar["sha256"])):
                raise ValidationError(f"upload {upload_id} checksum mismatch")
            stat = self.stat(
                namespace=str(sidecar["namespace"]), sha256=str(sidecar["sha256"])
            )
            assert stat is not None
            return stat
        finally:
            self._delete_sidecar(upload_id=upload_id)

    def presign_download(
        self, *, namespace: str, sha256: str, expires_in: int
    ) -> dict[str, Any]:
        _validate_keys(namespace=namespace, sha256=sha256)
        key = self._key(namespace=namespace, sha256=sha256)
        if self._head(key=key) is None:
            raise NotFoundError(f"object not found: {namespace}/{sha256}")
        return {
            "url": self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=int(expires_in),
            )
        }

    def stat(self, *, namespace: str, sha256: str) -> ObjectStat | None:
        _validate_keys(namespace=namespace, sha256=sha256)
        head = self._head(key=self._key(namespace=namespace, sha256=sha256))
        if head is None:
            return None
        meta = head.get("Metadata") or {}
        return ObjectStat(
            sha256=sha256,
            namespace=namespace,
            size_bytes=int(head.get("ContentLength") or 0),
            content_type=str(head.get("ContentType") or "application/octet-stream"),
            created_at=str(meta.get("created_at") or now_iso()),
        )

    def delete(self, *, namespace: str, sha256: str) -> bool:
        _validate_keys(namespace=namespace, sha256=sha256)
        key = self._key(namespace=namespace, sha256=sha256)
        existed = self._head(key=key) is not None
        self._s3.delete_object(Bucket=self.bucket, Key=key)
        return existed

    def _key(self, *, namespace: str, sha256: str) -> str:
        return f"{namespace}/{sha256}"

    def _meta_key(self, *, upload_id: str) -> str:
        return f"{_UPLOAD_PREFIX}{upload_id}.meta"

    def _part_size(self, *, size_bytes: int) -> int:
        return max(self.multipart_part_bytes, math.ceil(size_bytes / 10000))

    def _put_sidecar(self, *, upload_id: str, sidecar: dict[str, Any]) -> None:
        self._s3.put_object(
            Bucket=self.bucket,
            Key=self._meta_key(upload_id=upload_id),
            Body=json.dumps(sidecar, sort_keys=True).encode("utf-8"),
        )

    def _get_sidecar(self, *, upload_id: str) -> dict[str, Any]:
        try:
            obj = self._s3.get_object(
                Bucket=self.bucket, Key=self._meta_key(upload_id=upload_id)
            )
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise NotFoundError(
                    f"unknown or already-consumed upload: {upload_id}"
                ) from exc
            raise
        return json.loads(obj["Body"].read().decode("utf-8"))

    def _complete_multipart(
        self, *, sidecar: dict[str, Any], parts: list[dict[str, Any]] | None
    ) -> None:
        if not parts:
            raise ValidationError(
                f"multipart upload needs completed parts: {sidecar['upload_id']}"
            )
        completed = []
        for part in parts:
            item = {
                "ETag": str(part.get("ETag") or part["etag"]),
                "PartNumber": int(part.get("PartNumber") or part["part_number"]),
            }
            completed.append(item)
        self._s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=self._key(namespace=str(sidecar["namespace"]), sha256=str(sidecar["sha256"])),
            UploadId=str(sidecar["s3_upload_id"]),
            MultipartUpload={"Parts": completed},
        )

    def _abort_multipart(self, *, sidecar: dict[str, Any]) -> None:
        try:
            self._s3.abort_multipart_upload(
                Bucket=self.bucket,
                Key=self._key(namespace=str(sidecar["namespace"]), sha256=str(sidecar["sha256"])),
                UploadId=str(sidecar["s3_upload_id"]),
            )
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found(exc):
                raise

    def _delete_sidecar(self, *, upload_id: str) -> None:
        self._s3.delete_object(
            Bucket=self.bucket, Key=self._meta_key(upload_id=upload_id)
        )

    def _head(self, *, key: str, checksum: bool = False) -> dict[str, Any] | None:
        try:
            kwargs = {"Bucket": self.bucket, "Key": key}
            if checksum:
                kwargs["ChecksumMode"] = "ENABLED"
            return self._s3.head_object(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise


def _sha256_b64(sha256: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256)).decode("ascii")


def _is_not_found(exc: Exception) -> bool:
    code = (
        getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if hasattr(exc, "response")
        else ""
    )
    return code in {"404", "NoSuchKey", "NotFound", "NoSuchUpload"} or exc.__class__.__name__ in {
        "NoSuchKey",
        "NoSuchUpload",
        "404",
    }
