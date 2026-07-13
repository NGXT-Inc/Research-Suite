"""ObjectStore contract for heavy-file provider implementations."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.utils import NotFoundError, ValidationError


class _S3NotFound(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class ObjectStoreContractMixin:
    """One behavioral suite run against every ObjectStore implementation."""

    def make_store(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def _write_upload(self, target: dict, data: bytes) -> None:
        from urllib.parse import urlsplit
        from urllib.request import url2pathname

        url = urlsplit(target["url"])
        self.assertEqual(url.scheme, "file")
        Path(url2pathname(url.path)).write_bytes(data)

    def _read_download(self, target: dict) -> bytes:
        import urllib.request

        with urllib.request.urlopen(target["url"]) as response:  # noqa: S310
            return response.read()

    def _upload(
        self, store, *, namespace: str, data: bytes, size_bytes: int | None = None
    ):
        sha = hashlib.sha256(data).hexdigest()
        target = store.presign_upload(
            namespace=namespace,
            sha256=sha,
            size_bytes=len(data) if size_bytes is None else size_bytes,
            content_type="text/plain",
            expires_in=300,
        )
        self._write_upload(target, data)
        return sha, store.complete_upload(upload_id=target["upload_id"])

    def test_presign_complete_round_trip(self) -> None:
        store = self.make_store()
        data = b"heavy object bytes"
        sha, stat = self._upload(store, namespace="proj_a", data=data)
        self.assertEqual(stat.sha256, sha)
        self.assertEqual(stat.namespace, "proj_a")
        self.assertEqual(stat.size_bytes, len(data))
        self.assertEqual(stat.content_type, "text/plain")
        self.assertEqual(
            self._read_download(
                store.presign_download(namespace="proj_a", sha256=sha, expires_in=300)
            ),
            data,
        )

    def test_identical_upload_is_idempotent(self) -> None:
        store = self.make_store()
        data = b"dedup me"
        sha1, stat1 = self._upload(store, namespace="proj_a", data=data)
        sha2, stat2 = self._upload(store, namespace="proj_a", data=data)
        self.assertEqual(sha2, sha1)
        self.assertEqual(stat2.sha256, stat1.sha256)
        self.assertEqual(stat2.size_bytes, stat1.size_bytes)

    def test_stat_reports_metadata(self) -> None:
        store = self.make_store()
        sha, _ = self._upload(store, namespace="proj_a", data=b"12345")
        stat = store.stat(namespace="proj_a", sha256=sha)
        self.assertIsNotNone(stat)
        self.assertEqual(stat.size_bytes, 5)
        self.assertEqual(stat.content_type, "text/plain")

    def test_delete(self) -> None:
        store = self.make_store()
        sha, _ = self._upload(store, namespace="proj_a", data=b"gone soon")
        self.assertTrue(store.delete(namespace="proj_a", sha256=sha))
        self.assertFalse(store.delete(namespace="proj_a", sha256=sha))
        self.assertIsNone(store.stat(namespace="proj_a", sha256=sha))
        with self.assertRaises(NotFoundError):
            store.presign_download(namespace="proj_a", sha256=sha, expires_in=300)

    def test_size_cap_rejection_consumes_upload(self) -> None:
        store = self.make_store()
        data = b"five!"
        sha = hashlib.sha256(data).hexdigest()
        target = store.presign_upload(
            namespace="proj_a", sha256=sha, size_bytes=4, expires_in=300
        )
        self._write_upload(target, data)
        with self.assertRaises(ValidationError):
            store.complete_upload(upload_id=target["upload_id"])
        with self.assertRaises(NotFoundError):
            store.complete_upload(upload_id=target["upload_id"])

    def test_checksum_mismatch_rejection_consumes_upload(self) -> None:
        store = self.make_store()
        from urllib.error import HTTPError

        data = b"actual"
        expected_sha = hashlib.sha256(b"expected").hexdigest()
        target = store.presign_upload(
            namespace="proj_a",
            sha256=expected_sha,
            size_bytes=len(data),
            expires_in=300,
        )
        try:
            self._write_upload(target, data)
        except HTTPError:
            pass
        else:
            with self.assertRaises(ValidationError):
                store.complete_upload(upload_id=target["upload_id"])

        self.assertIsNone(store.stat(namespace="proj_a", sha256=expected_sha))
        with self.assertRaises(NotFoundError):
            store.presign_download(
                namespace="proj_a", sha256=expected_sha, expires_in=300
            )

    def test_abort_upload_is_idempotent_and_consumes_upload(self) -> None:
        store = self.make_store()
        target = store.presign_upload(
            namespace="proj_a",
            sha256=hashlib.sha256(b"cancel").hexdigest(),
            size_bytes=6,
            expires_in=300,
        )

        self.assertTrue(store.abort_upload(upload_id=target["upload_id"]))
        self.assertFalse(store.abort_upload(upload_id=target["upload_id"]))
        with self.assertRaises(NotFoundError):
            store.complete_upload(upload_id=target["upload_id"])


class S3CompatibleObjectStoreVerificationTest(unittest.TestCase):
    def test_presigned_uploads_sign_exact_content_lengths(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        client = MagicMock()
        client.generate_presigned_url.return_value = "https://upload.test"
        client.create_multipart_upload.return_value = {"UploadId": "multi"}
        store = S3CompatibleObjectStore(
            bucket="bucket",
            client=client,
            multipart_threshold_bytes=6,
            multipart_part_bytes=5,
        )
        sha = hashlib.sha256(b"123456").hexdigest()

        store.presign_upload(
            namespace="proj_a", sha256=sha, size_bytes=6, expires_in=300
        )
        self.assertEqual(
            client.generate_presigned_url.call_args.kwargs["Params"]["ContentLength"],
            6,
        )
        client.generate_presigned_url.reset_mock()
        store.presign_upload(
            namespace="proj_a", sha256=sha, size_bytes=11, expires_in=300
        )
        self.assertEqual(
            [
                call.kwargs["Params"]["ContentLength"]
                for call in client.generate_presigned_url.call_args_list
            ],
            [5, 5, 1],
        )

    def test_single_checksum_rejection_removes_only_quarantine(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        expected_sha = hashlib.sha256(b"right!").hexdigest()
        sidecar = {
            "upload_id": "upload_test",
            "namespace": "proj_a",
            "sha256": expected_sha,
            "size_bytes": 6,
            "content_type": "application/octet-stream",
            "mode": "single",
        }
        client = MagicMock()
        client.get_object.return_value = {
            "Body": BytesIO(json.dumps(sidecar).encode("utf-8"))
        }
        client.head_object.return_value = {
            "ContentLength": 6,
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(b"wrong!").digest()).decode(),
        }
        store = S3CompatibleObjectStore(bucket="bucket", client=client)

        with self.assertRaisesRegex(ValidationError, "checksum mismatch"):
            store.complete_upload(upload_id="upload_test")

        deleted = [call.kwargs["Key"] for call in client.delete_object.call_args_list]
        self.assertEqual(
            deleted, [".uploads/upload_test.data", ".uploads/upload_test.meta"]
        )

    def test_multipart_rejects_same_size_wrong_bytes_before_promotion(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        expected_sha = hashlib.sha256(b"right!").hexdigest()
        sidecar = {
            "upload_id": "upload_test",
            "namespace": "proj_a",
            "sha256": expected_sha,
            "size_bytes": 6,
            "content_type": "application/octet-stream",
            "mode": "multipart",
            "s3_upload_id": "multipart_test",
        }
        client = MagicMock()
        client.get_object.side_effect = [
            {"Body": BytesIO(json.dumps(sidecar).encode("utf-8"))},
            {"Body": BytesIO(b"wrong!")},
        ]
        client.head_object.return_value = {"ContentLength": 6}
        store = S3CompatibleObjectStore(bucket="bucket", client=client)

        with self.assertRaisesRegex(ValidationError, "checksum mismatch"):
            store.complete_upload(
                upload_id="upload_test",
                parts=[{"PartNumber": 1, "ETag": "etag"}],
            )

        client.upload_fileobj.assert_not_called()
        deleted = [call.kwargs["Key"] for call in client.delete_object.call_args_list]
        self.assertEqual(
            deleted,
            [".uploads/upload_test.data", ".uploads/upload_test.meta"],
        )

    def test_single_promotion_failure_preserves_upload_for_retry(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        data = b"verified"
        sha = hashlib.sha256(data).hexdigest()
        sidecar = {
            "upload_id": "upload_test",
            "namespace": "proj_a",
            "sha256": sha,
            "size_bytes": len(data),
            "content_type": "application/octet-stream",
            "mode": "single",
        }
        final = {"bytes": b"trusted-existing"}
        client = MagicMock()

        def get_object(*, Key, **_kwargs):
            body = json.dumps(sidecar).encode() if Key.endswith(".meta") else data
            return {"Body": BytesIO(body)}

        def head_object(*, Key, **_kwargs):
            if Key.endswith(".data"):
                return {
                    "ContentLength": len(data),
                    "ChecksumSHA256": base64.b64encode(bytes.fromhex(sha)).decode(),
                }
            return {"ContentLength": len(final["bytes"])}

        attempts = 0

        def promote(body, _bucket, _key, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("provider unavailable")
            final["bytes"] = body.read()

        client.get_object.side_effect = get_object
        client.head_object.side_effect = head_object
        client.upload_fileobj.side_effect = promote
        store = S3CompatibleObjectStore(bucket="bucket", client=client)

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            store.complete_upload(upload_id="upload_test")
        self.assertEqual(final["bytes"], b"trusted-existing")
        client.delete_object.assert_not_called()

        stat = store.complete_upload(upload_id="upload_test")

        self.assertEqual(stat.sha256, sha)
        self.assertEqual(final["bytes"], data)
        self.assertEqual(
            [call.kwargs["Key"] for call in client.delete_object.call_args_list],
            [".uploads/upload_test.data", ".uploads/upload_test.meta"],
        )

    def test_multipart_hash_failure_retries_after_provider_completion(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        data = b"verified"
        sha = hashlib.sha256(data).hexdigest()
        sidecar = {
            "upload_id": "upload_test",
            "namespace": "proj_a",
            "sha256": sha,
            "size_bytes": len(data),
            "content_type": "application/octet-stream",
            "mode": "multipart",
            "s3_upload_id": "multipart_test",
        }
        state = {"assembled": False, "hash_reads": 0, "final": b"trusted-existing"}
        client = MagicMock()

        def complete_multipart(**_kwargs):
            state["assembled"] = True

        def head_object(*, Key, **_kwargs):
            if Key.endswith(".data"):
                if not state["assembled"]:
                    raise _S3NotFound()
                return {"ContentLength": len(data)}
            return {"ContentLength": len(state["final"])}

        def get_object(*, Key, **_kwargs):
            if Key.endswith(".meta"):
                return {"Body": BytesIO(json.dumps(sidecar).encode())}
            state["hash_reads"] += 1
            if state["hash_reads"] == 1:
                raise RuntimeError("hash read unavailable")
            return {"Body": BytesIO(data)}

        def promote(body, _bucket, _key, **_kwargs):
            state["final"] = body.read()

        client.complete_multipart_upload.side_effect = complete_multipart
        client.head_object.side_effect = head_object
        client.get_object.side_effect = get_object
        client.upload_fileobj.side_effect = promote
        store = S3CompatibleObjectStore(bucket="bucket", client=client)
        parts = [{"PartNumber": 1, "ETag": "etag"}]

        with self.assertRaisesRegex(RuntimeError, "hash read unavailable"):
            store.complete_upload(upload_id="upload_test", parts=parts)
        self.assertEqual(state["final"], b"trusted-existing")
        client.delete_object.assert_not_called()

        stat = store.complete_upload(upload_id="upload_test")

        self.assertEqual(stat.sha256, sha)
        self.assertEqual(state["final"], data)
        self.assertEqual(client.complete_multipart_upload.call_count, 1)
        client.abort_multipart_upload.assert_not_called()
        self.assertEqual(
            [call.kwargs["Key"] for call in client.delete_object.call_args_list],
            [".uploads/upload_test.data", ".uploads/upload_test.meta"],
        )

    def test_missing_quarantine_consumes_sidecar(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        sidecar = {
            "upload_id": "upload_test",
            "namespace": "proj_a",
            "sha256": hashlib.sha256(b"data").hexdigest(),
            "size_bytes": 4,
            "content_type": "application/octet-stream",
            "mode": "single",
        }
        client = MagicMock()
        client.get_object.return_value = {
            "Body": BytesIO(json.dumps(sidecar).encode())
        }
        client.head_object.side_effect = _S3NotFound()
        store = S3CompatibleObjectStore(bucket="bucket", client=client)

        with self.assertRaisesRegex(NotFoundError, "received no bytes"):
            store.complete_upload(upload_id="upload_test")

        self.assertEqual(
            [call.kwargs["Key"] for call in client.delete_object.call_args_list],
            [".uploads/upload_test.data", ".uploads/upload_test.meta"],
        )

    def test_abort_multipart_cleans_provider_upload_and_sidecars(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        sidecar = {
            "upload_id": "upload_test",
            "namespace": "proj_a",
            "sha256": hashlib.sha256(b"data").hexdigest(),
            "size_bytes": 4,
            "content_type": "application/octet-stream",
            "mode": "multipart",
            "s3_upload_id": "multipart_test",
        }
        client = MagicMock()
        client.get_object.return_value = {
            "Body": BytesIO(json.dumps(sidecar).encode("utf-8"))
        }
        store = S3CompatibleObjectStore(bucket="bucket", client=client)

        self.assertTrue(store.abort_upload(upload_id="upload_test"))

        client.abort_multipart_upload.assert_called_once_with(
            Bucket="bucket",
            Key=".uploads/upload_test.data",
            UploadId="multipart_test",
        )
        self.assertEqual(
            [call.kwargs["Key"] for call in client.delete_object.call_args_list],
            [".uploads/upload_test.data", ".uploads/upload_test.meta"],
        )

    def test_abort_failure_preserves_multipart_sidecars_for_retry(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        sidecar = {
            "upload_id": "upload_test",
            "namespace": "proj_a",
            "sha256": hashlib.sha256(b"data").hexdigest(),
            "size_bytes": 4,
            "content_type": "application/octet-stream",
            "mode": "multipart",
            "s3_upload_id": "multipart_test",
        }
        client = MagicMock()
        client.get_object.return_value = {
            "Body": BytesIO(json.dumps(sidecar).encode("utf-8"))
        }
        client.abort_multipart_upload.side_effect = RuntimeError("provider unavailable")
        store = S3CompatibleObjectStore(bucket="bucket", client=client)

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            store.abort_upload(upload_id="upload_test")

        client.delete_object.assert_not_called()


class S3CompatibleObjectStoreClientConfigTest(unittest.TestCase):
    def test_boto3_client_receives_explicit_credentials_when_both_set(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        captured = {}
        fake_boto3 = types.SimpleNamespace(
            client=lambda service, **kwargs: captured.setdefault(
                "call", {"service": service, "kwargs": kwargs}
            )
        )

        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            store = S3CompatibleObjectStore(
                bucket="bucket",
                endpoint_url="https://x",
                region_name="auto",
                access_key_id="AKIA...",
                secret_access_key="shh",
            )

        self.assertIs(store._s3, captured["call"])
        self.assertEqual(captured["call"]["service"], "s3")
        self.assertEqual(
            captured["call"]["kwargs"],
            {
                "endpoint_url": "https://x",
                "region_name": "auto",
                "aws_access_key_id": "AKIA...",
                "aws_secret_access_key": "shh",
            },
        )

    def test_boto3_client_omits_credentials_unless_both_set(self) -> None:
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        calls = []
        fake_boto3 = types.SimpleNamespace(
            client=lambda service, **kwargs: calls.append(
                {"service": service, "kwargs": kwargs}
            )
            or calls[-1]
        )

        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            S3CompatibleObjectStore(
                bucket="bucket",
                endpoint_url="https://x",
                region_name="auto",
            )
            S3CompatibleObjectStore(
                bucket="bucket",
                endpoint_url="https://x",
                region_name="auto",
                access_key_id="AKIA...",
            )

        for call in calls:
            self.assertEqual(call["service"], "s3")
            self.assertEqual(
                call["kwargs"], {"endpoint_url": "https://x", "region_name": "auto"}
            )
            self.assertNotIn("aws_access_key_id", call["kwargs"])
            self.assertNotIn("aws_secret_access_key", call["kwargs"])


try:
    from tests.state import test_s3_blob_store as s3_fixture
except ImportError:  # pragma: no cover - depends on optional test module layout
    s3_fixture = None


def setUpModule() -> None:
    if s3_fixture is not None:
        s3_fixture.setUpModule()


def tearDownModule() -> None:
    if s3_fixture is not None:
        s3_fixture.tearDownModule()


@unittest.skipIf(
    s3_fixture is None or not (s3_fixture.HAVE_DOCKER and s3_fixture.HAVE_BOTO3),
    "dockerized minio blob-store fixture or boto3 unavailable",
)
class S3CompatibleObjectStoreContractTest(ObjectStoreContractMixin, unittest.TestCase):
    _bucket_seq = 0

    def make_store(self, **kwargs):
        from backend.storage.s3_object_store import S3CompatibleObjectStore

        assert s3_fixture is not None
        assert s3_fixture._endpoint is not None
        client = s3_fixture._make_client(s3_fixture._endpoint)
        type(self)._bucket_seq += 1
        bucket = f"rp-objects-{type(self)._bucket_seq}"
        client.create_bucket(Bucket=bucket)
        return S3CompatibleObjectStore(bucket=bucket, client=client, **kwargs)

    def _write_upload(self, target: dict, data: bytes) -> None:
        import urllib.request

        req = urllib.request.Request(
            target["url"],
            data=data,
            method="PUT",
            headers={
                "Content-Type": target.get("content_type", "application/octet-stream"),
                "x-amz-checksum-sha256": base64.b64encode(
                    hashlib.sha256(data).digest()
                ).decode("ascii"),
            },
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            self.assertIn(resp.status, (200, 204))

    def test_multipart_upload_completes_to_content_key(self) -> None:
        import httpx

        part_size = 5 * 1024 * 1024
        store = self.make_store(
            multipart_threshold_bytes=1,
            multipart_part_bytes=part_size,
        )
        data = (b"a" * part_size) + b"tail"
        sha = hashlib.sha256(data).hexdigest()
        target = store.presign_upload(
            namespace="proj_a",
            sha256=sha,
            size_bytes=len(data),
            content_type="application/octet-stream",
            expires_in=300,
        )
        completed_parts = []
        # urllib trips on a 5 MiB PUT to MinIO (no Expect: 100-continue); httpx
        # (botocore's own HTTP client) rides the presigned part seam cleanly.
        with httpx.Client(timeout=60) as client:
            for part in target["parts"]:
                part_number = int(part["part_number"])
                start = (part_number - 1) * part_size
                chunk = data[start : start + part_size]
                resp = client.put(part["url"], content=chunk)
                resp.raise_for_status()
                completed_parts.append(
                    {"PartNumber": part_number, "ETag": resp.headers["ETag"]}
                )

        stat = store.complete_upload(upload_id=target["upload_id"], parts=completed_parts)

        self.assertEqual(stat.sha256, sha)
        self.assertEqual(stat.size_bytes, len(data))
        self.assertIsNotNone(store.stat(namespace="proj_a", sha256=sha))
        uploads = store._s3.list_objects_v2(Bucket=store.bucket, Prefix=".uploads/")
        self.assertEqual(uploads.get("Contents", []), [])


if __name__ == "__main__":
    unittest.main()
