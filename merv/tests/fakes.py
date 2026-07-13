from __future__ import annotations


class FakeProcess:
    def __init__(self, stdout: str = "", code: int = 0, *, running: bool = True) -> None:
        self._stdout = stdout
        self._code = code
        self._running = running
        self.terminated = False
        self.killed = False

    @property
    def stdout(self):
        text = self._stdout

        class _Stream:
            def read(self_inner):
                return text

        return _Stream()

    @property
    def stderr(self):
        return None

    def poll(self) -> int | None:
        if self.terminated or self.killed:
            return -15
        return None if self._running else self._code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return self._code if not (self.terminated or self.killed) else -15
class FakeBlobStore:
    """In-memory BlobStore double sharing LocalDirBlobStore's semantics.

    Single-use uploads stage in a real temp directory so the ``file://`` URL
    the presign returns is writable by producers exactly like the local
    store's.
    """

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.meta: dict[tuple[str, str], dict] = {}
        self.uploads: dict[str, dict] = {}
        self._staging_dir: str | None = None

    def put(
        self,
        *,
        namespace: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        expires_at: str | None = None,
    ) -> str:
        import hashlib

        from backend.utils import now_iso

        sha = hashlib.sha256(data).hexdigest()
        key = (namespace, sha)
        if key in self.blobs:
            current = self.meta[key].get("expires_at")
            if current is not None and (expires_at is None or expires_at > current):
                self.meta[key]["expires_at"] = expires_at
            return sha
        self.blobs[key] = data
        self.meta[key] = {
            "sha256": sha,
            "namespace": namespace,
            "size_bytes": len(data),
            "content_type": content_type,
            "created_at": now_iso(),
            "expires_at": expires_at,
        }
        return sha

    def get(self, *, namespace: str, sha256: str) -> bytes:
        from backend.utils import NotFoundError

        key = (namespace, sha256)
        if key not in self.blobs:
            raise NotFoundError(f"blob not found: {namespace}/{sha256}")
        return self.blobs[key]

    def presign_get(self, *, namespace: str, sha256: str) -> dict:
        import tempfile
        from pathlib import Path as _Path

        data = self.get(namespace=namespace, sha256=sha256)
        if self._staging_dir is None:
            self._staging_dir = tempfile.mkdtemp(prefix="fake-blob-uploads-")
        path = _Path(self._staging_dir) / f"{namespace}-{sha256}"
        path.write_bytes(data)
        return {"url": path.resolve().as_uri()}

    def stat(self, *, namespace: str, sha256: str):
        from backend.storage.blobs import BlobStat

        meta = self.meta.get((namespace, sha256))
        if meta is None:
            return None
        return BlobStat(**meta)

    def delete(self, *, namespace: str, sha256: str) -> bool:
        key = (namespace, sha256)
        existed = key in self.blobs
        self.blobs.pop(key, None)
        self.meta.pop(key, None)
        return existed

    def presign_put(
        self,
        *,
        namespace: str,
        max_size_bytes: int,
        expires_at: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict:
        import tempfile
        from pathlib import Path as _Path

        from backend.utils import new_id

        if self._staging_dir is None:
            self._staging_dir = tempfile.mkdtemp(prefix="fake-blob-uploads-")
        upload_id = new_id(prefix="upload")
        staging = _Path(self._staging_dir) / upload_id
        self.uploads[upload_id] = {
            "namespace": namespace,
            "max_size_bytes": int(max_size_bytes),
            "content_type": content_type,
            "expires_at": expires_at,
            "path": staging,
        }
        return {
            "upload_id": upload_id,
            "url": staging.resolve().as_uri(),
            "max_size_bytes": int(max_size_bytes),
            "expires_at": expires_at,
        }

    def finalize_put(self, *, upload_id: str):
        from backend.utils import NotFoundError, ValidationError

        meta = self.uploads.pop(upload_id, None)
        if meta is None:
            raise NotFoundError(f"unknown or already-consumed upload: {upload_id}")
        staging = meta["path"]
        try:
            if not staging.exists():
                raise NotFoundError(f"upload received no bytes: {upload_id}")
            data = staging.read_bytes()
            if len(data) > meta["max_size_bytes"]:
                raise ValidationError(
                    f"upload {upload_id} exceeds its size cap: "
                    f"{len(data)} > {meta['max_size_bytes']} bytes"
                )
            sha = self.put(
                namespace=meta["namespace"],
                data=data,
                content_type=meta["content_type"],
                expires_at=meta["expires_at"],
            )
        finally:
            try:
                staging.unlink()
            except FileNotFoundError:
                pass
        stat = self.stat(namespace=meta["namespace"], sha256=sha)
        assert stat is not None
        return stat

    def sweep_expired(self, *, now: str | None = None) -> int:
        from backend.utils import now_iso

        cutoff = now or now_iso()
        expired = [
            key
            for key, meta in self.meta.items()
            if meta.get("expires_at") and str(meta["expires_at"]) <= cutoff
        ]
        for key in expired:
            self.blobs.pop(key, None)
            self.meta.pop(key, None)
        return len(expired)


class FakeObjectStore:
    """Test double for the heavy ObjectStore port.

    Production storage now goes exclusively through S3CompatibleObjectStore.
    This fake exists only to keep ledger/service tests isolated from Docker.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.meta: dict[tuple[str, str], dict] = {}
        self.uploads: dict[str, dict] = {}
        self._staging_dir: str | None = None

    def presign_upload(
        self,
        *,
        namespace: str,
        sha256: str,
        size_bytes: int,
        content_type: str = "application/octet-stream",
        expires_in: int,
    ) -> dict:
        import tempfile
        from pathlib import Path as _Path

        from backend.storage.blobs import _validate_keys
        from backend.utils import new_id, now_iso

        _validate_keys(namespace=namespace, sha256=sha256)
        if self._staging_dir is None:
            self._staging_dir = tempfile.mkdtemp(prefix="fake-object-uploads-")
        upload_id = new_id(prefix="upload")
        staging = _Path(self._staging_dir) / upload_id
        self.uploads[upload_id] = {
            "namespace": namespace,
            "sha256": sha256,
            "size_bytes": int(size_bytes),
            "content_type": content_type,
            "created_at": now_iso(),
            "path": staging,
        }
        return {
            "upload_id": upload_id,
            "url": staging.resolve().as_uri(),
            "size_bytes": int(size_bytes),
            "content_type": content_type,
            "expires_in": int(expires_in),
        }

    def complete_upload(self, *, upload_id: str, parts: list[dict] | None = None):
        import hashlib

        from backend.ports.object_store import ObjectStat
        from backend.utils import NotFoundError, ValidationError, now_iso

        _ = parts
        meta = self.uploads.pop(upload_id, None)
        if meta is None:
            raise NotFoundError(f"unknown or already-consumed upload: {upload_id}")
        staging = meta["path"]
        try:
            if not staging.exists():
                raise NotFoundError(f"upload received no bytes: {upload_id}")
            data = staging.read_bytes()
            if len(data) > int(meta["size_bytes"]):
                raise ValidationError(
                    f"upload {upload_id} exceeds its size cap: "
                    f"{len(data)} > {meta['size_bytes']} bytes"
                )
            sha = hashlib.sha256(data).hexdigest()
            if sha != str(meta["sha256"]):
                raise ValidationError(
                    f"upload {upload_id} checksum mismatch: "
                    f"expected {meta['sha256']}, got {sha}"
                )
            key = (str(meta["namespace"]), sha)
            self.objects.setdefault(key, data)
            self.meta.setdefault(
                key,
                {
                    "sha256": sha,
                    "namespace": str(meta["namespace"]),
                    "size_bytes": len(data),
                    "content_type": str(meta["content_type"]),
                    "created_at": now_iso(),
                },
            )
            return ObjectStat(**self.meta[key])
        finally:
            try:
                staging.unlink()
            except FileNotFoundError:
                pass

    def abort_upload(self, *, upload_id: str) -> bool:
        meta = self.uploads.get(upload_id)
        if meta is None:
            return False
        try:
            meta["path"].unlink()
        except FileNotFoundError:
            pass
        self.uploads.pop(upload_id)
        return True

    def presign_download(self, *, namespace: str, sha256: str, expires_in: int) -> dict:
        import tempfile
        from pathlib import Path as _Path

        from backend.utils import NotFoundError

        key = (namespace, sha256)
        if key not in self.objects:
            raise NotFoundError(f"object not found: {namespace}/{sha256}")
        if self._staging_dir is None:
            self._staging_dir = tempfile.mkdtemp(prefix="fake-object-uploads-")
        path = _Path(self._staging_dir) / f"{namespace}-{sha256}"
        path.write_bytes(self.objects[key])
        return {"url": path.resolve().as_uri(), "expires_in": int(expires_in)}

    def stat(self, *, namespace: str, sha256: str):
        from backend.ports.object_store import ObjectStat

        meta = self.meta.get((namespace, sha256))
        return ObjectStat(**meta) if meta is not None else None

    def delete(self, *, namespace: str, sha256: str) -> bool:
        key = (namespace, sha256)
        existed = key in self.objects
        self.objects.pop(key, None)
        self.meta.pop(key, None)
        return existed
