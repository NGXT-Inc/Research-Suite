from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.execution.ssh_rsync import SshRsyncResult


class FakeRsyncSyncer:
    def __init__(
        self,
        *,
        sync_pulled: int = 2,
        push_pulled: int = 1,
        duration_seconds: float = 0.1,
        command_count: int = 2,
        sync_stdout: str = "small.txt\n",
        push_stdout: str = "seed.txt\n",
        sync_stderr: str = "",
        push_stderr: str = "",
    ) -> None:
        self.sync_pulled = sync_pulled
        self.push_pulled = push_pulled
        self.duration_seconds = duration_seconds
        self.command_count = command_count
        self.sync_stdout = sync_stdout
        self.push_stdout = push_stdout
        self.sync_stderr = sync_stderr
        self.push_stderr = push_stderr
        self.calls: list[dict] = []
        self.push_calls: list[dict] = []

    def sync(self, **kwargs) -> SshRsyncResult:
        self.calls.append(dict(kwargs))
        return SshRsyncResult(
            pulled=self.sync_pulled,
            duration_seconds=self.duration_seconds,
            local_dir=str(kwargs["local_sync_dir"]),
            remote_dir=str(kwargs["remote_sync_dir"]),
            command_count=self.command_count,
            stdout=self.sync_stdout,
            stderr=self.sync_stderr,
        )

    def push_initial(self, **kwargs) -> SshRsyncResult:
        self.push_calls.append(dict(kwargs))
        return SshRsyncResult(
            pulled=self.push_pulled,
            duration_seconds=self.duration_seconds,
            local_dir=str(kwargs["local_sync_dir"]),
            remote_dir=str(kwargs["remote_sync_dir"]),
            command_count=self.command_count,
            stdout=self.push_stdout,
            stderr=self.push_stderr,
            direction="push",
        )


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


def write_fake_mlflow_db(path: Path, *, with_run: bool = True) -> None:
    """Minimal slice of MLflow's SQLAlchemy schema (verified against 2.18)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE experiments (
            experiment_id INTEGER PRIMARY KEY, name TEXT,
            lifecycle_stage TEXT DEFAULT 'active', last_update_time BIGINT
        );
        CREATE TABLE runs (
            run_uuid TEXT PRIMARY KEY, name TEXT, status TEXT,
            start_time BIGINT, end_time BIGINT,
            lifecycle_stage TEXT DEFAULT 'active', experiment_id INTEGER
        );
        CREATE TABLE params (key TEXT, value TEXT, run_uuid TEXT);
        CREATE TABLE latest_metrics (
            key TEXT, value FLOAT, timestamp BIGINT, step BIGINT,
            is_nan BOOLEAN, run_uuid TEXT
        );
        CREATE TABLE metrics (
            key TEXT, value FLOAT, timestamp BIGINT, run_uuid TEXT,
            step BIGINT DEFAULT 0, is_nan BOOLEAN DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO experiments VALUES (0, 'Default', 'active', 1)")
    conn.execute("INSERT INTO experiments VALUES (1, 'lora_glue', 'active', 99)")
    if with_run:
        conn.execute(
            "INSERT INTO runs VALUES ('r1', 'seed_0', 'FINISHED', 100, 200, 'active', 1)"
        )
        conn.execute("INSERT INTO params VALUES ('lr', '0.0005', 'r1')")
        conn.execute("INSERT INTO latest_metrics VALUES ('acc', 0.91, 6, 20, 0, 'r1')")
        conn.execute("INSERT INTO metrics VALUES ('acc', 0.85, 5, 'r1', 10, 0)")
        conn.execute("INSERT INTO metrics VALUES ('acc', 0.91, 6, 'r1', 20, 0)")
    conn.commit()
    conn.close()


class FakeBlobStore:
    """In-memory BlobStore double sharing LocalDirBlobStore's semantics."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.meta: dict[tuple[str, str], dict] = {}

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

    def stat(self, *, namespace: str, sha256: str):
        from backend.state.blobs import BlobStat

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
