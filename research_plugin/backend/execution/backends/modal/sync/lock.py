"""Cross-process locking for Modal volume sync."""

from __future__ import annotations

import errno
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is available on supported platforms.
    fcntl = None  # type: ignore[assignment]


_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class InterProcessSyncLock:
    """Repo-wide file lock used to serialize sync passes.

    The file lock protects independent Python processes. The shared in-process
    lock handles same-process contenders, including platforms whose file-lock
    semantics are process-scoped.
    """

    def __init__(self, *, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._thread_lock = _thread_lock_for(lock_path)

    @contextmanager
    def acquire(self, *, blocking: bool = True) -> Iterator[bool]:
        acquired_thread_lock = self._thread_lock.acquire(blocking=blocking)
        if not acquired_thread_lock:
            yield False
            return

        lock_file: TextIO | None = None
        acquired_file_lock = False
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self.lock_path.open("a+", encoding="utf-8")
            if fcntl is not None:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(lock_file.fileno(), flags)
                    acquired_file_lock = True
                except OSError as exc:
                    if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                        yield False
                        return
                    raise
            yield True
        finally:
            if acquired_file_lock and lock_file is not None and fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            if lock_file is not None:
                lock_file.close()
            self._thread_lock.release()


def _thread_lock_for(lock_path: Path) -> threading.Lock:
    key = lock_path.resolve()
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock
