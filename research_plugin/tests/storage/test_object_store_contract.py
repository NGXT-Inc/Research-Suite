"""ObjectStore contract for heavy-file provider implementations."""

from __future__ import annotations

import base64
import hashlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.utils import NotFoundError, ValidationError


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
