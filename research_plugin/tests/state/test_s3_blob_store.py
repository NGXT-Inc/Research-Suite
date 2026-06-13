"""S3BlobStore against the SAME blob contract suite, over a dockerized minio.

The cloud blob store (plan Phase 8) implements the identical BlobStore protocol
as LocalDirBlobStore, so artifact submission, figures, metrics, and the
parachute all work in the cloud unchanged. We run the existing
``BlobStoreContractMixin`` against a real S3-compatible server (minio) so the
parachute's presign_put is a real single-use HTTPS PUT — the thing a sandbox VM
can actually reach. Skips cleanly when docker or boto3 is unavailable.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import unittest

from tests.state.test_blob_store import BlobStoreContractMixin


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _boto3_available() -> bool:
    try:
        import boto3  # noqa: F401

        return True
    except ImportError:
        return False


HAVE_DOCKER = _docker_available()
HAVE_BOTO3 = _boto3_available()
CONTAINER = "rp-test-minio"
ACCESS_KEY = "rptestkey"
SECRET_KEY = "rptestsecret"
BUCKET = "rp-blobs"

_endpoint: str | None = None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_client(endpoint: str):
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
    )


def setUpModule() -> None:
    global _endpoint
    if not (HAVE_DOCKER and HAVE_BOTO3):
        return
    port = _free_port()
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", CONTAINER,
            "-e", f"MINIO_ROOT_USER={ACCESS_KEY}",
            "-e", f"MINIO_ROOT_PASSWORD={SECRET_KEY}",
            "-p", f"127.0.0.1:{port}:9000",
            "minio/minio", "server", "/data",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    endpoint = f"http://127.0.0.1:{port}"
    client = _make_client(endpoint)
    deadline = time.monotonic() + 60
    while True:
        try:
            client.list_buckets()
            break
        except Exception:  # noqa: BLE001
            if time.monotonic() > deadline:
                subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
                raise unittest.SkipTest("minio container never became ready")
            time.sleep(0.5)
    _endpoint = endpoint


def tearDownModule() -> None:
    if HAVE_DOCKER and HAVE_BOTO3:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


@unittest.skipUnless(HAVE_DOCKER and HAVE_BOTO3, "docker or boto3 unavailable")
class S3BlobStoreContractTest(BlobStoreContractMixin, unittest.TestCase):
    _bucket_seq = 0

    def make_store(self):
        from backend.state.s3_blobs import S3BlobStore

        assert _endpoint is not None
        client = _make_client(_endpoint)
        # A fresh bucket per store keeps the contract's namespace-isolation and
        # sweep assertions independent across tests.
        type(self)._bucket_seq += 1
        bucket = f"{BUCKET}-{type(self)._bucket_seq}-{int(time.time() * 1000) % 100000}"
        client.create_bucket(Bucket=bucket)
        return S3BlobStore(bucket=bucket, client=client)

    def _write_upload(self, target: dict, data: bytes) -> None:
        # Override the local file:// writer: PUT to the real presigned HTTPS URL
        # exactly as an off-process producer (the sandbox VM) would.
        import urllib.request

        req = urllib.request.Request(
            target["url"], data=data, method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310 — presigned URL from our store
            self.assertIn(resp.status, (200, 204))


if __name__ == "__main__":
    unittest.main()
