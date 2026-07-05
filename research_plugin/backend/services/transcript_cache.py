"""Control-side transcript cursor cache (cloud plan Phase 9, risk 14).

The UI is pure 3-second polling and ``sandbox.terminal`` is a management-key SSH
read of the whole transcript. With N viewers that is N SSH reads every 3s per
sandbox — the poll-amplification the plan flags. This bounded, TTL'd cache holds
the last full transcript per sandbox so repeated reads within the TTL serve from
memory instead of re-hitting the backend/SSH. ``since=<cursor>`` is then applied
to the cached bytes, so incremental polls stay correct AND cheap.

Deliberately tiny: an in-process dict with an LRU-ish bound and a per-entry TTL.
Not a distributed cache (one control process per deployment in v1); a viewer that
needs sub-TTL freshness can pass ``fresh=True`` to bypass. SSE/push is the real
fix and stays backlog (plan §4 Phase 9). The cache stores transcript bytes only
— no credentials, no keys.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


# Default freshness window. Comfortably above the 3 s UI poll so concurrent
# viewers coalesce, short enough that a single viewer sees near-live output.
DEFAULT_TTL_SECONDS = 2.0
# Bound the cache so a long-lived control plane with many sandboxes can't grow
# it without limit; the least-recently-stored entry is evicted past this.
DEFAULT_MAX_ENTRIES = 256


@dataclass
class _Entry:
    text: str
    stored_at: float


class TranscriptCache:
    """Per-sandbox last-transcript cache, bounded + TTL'd, clock-injectable."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock or time.monotonic
        self._entries: dict[str, _Entry] = {}
        # Concurrent viewers hit this from the server threadpool; guard the
        # dict (eviction iterates it). The expensive read() stays unlocked.
        self._lock = threading.Lock()
        # Cheap observability for the cache-hit test / metrics.
        self.hits = 0
        self.misses = 0

    def get_or_read(
        self,
        *,
        sandbox_id: str,
        read: Callable[[], str],
        since: int | None = None,
        fresh: bool = False,
    ) -> str:
        """Return the cached transcript, or call ``read`` and cache the result.

        ``read`` is the (expensive) backend/SSH fetch; it is invoked only on a
        miss, an expired entry, ``fresh=True``, or a cursor-driven refresh.

        Cursor awareness (correctness over the TTL): when ``since`` is given and
        it is at/beyond the cached transcript's end, the caller has already
        consumed everything cached, so we MUST read fresh to see new output —
        otherwise a fast-progressing sandbox's new bytes would be hidden until
        the TTL lapsed. A hit is served only when the caller's cursor still
        points strictly INTO cached content (the coalescing case: many viewers
        at the same earlier cursor), or there is no ``since`` (a tail poll) and
        the entry is within TTL. An empty ``sandbox_id`` always reads.
        """
        if not sandbox_id:
            self.misses += 1
            return read()
        now = self._clock()
        with self._lock:
            entry = self._entries.get(sandbox_id)
            fresh_window = entry is not None and (now - entry.stored_at) < self.ttl_seconds
            cursor_in_cache = (
                entry is not None and since is not None and int(since) < len(entry.text)
            )
            servable = entry is not None and not fresh and fresh_window and (
                since is None or cursor_in_cache
            )
            if servable:
                assert entry is not None
                self.hits += 1
                return entry.text
            self.misses += 1
        text = read()
        with self._lock:
            self._store(sandbox_id=sandbox_id, text=text, now=now)
        return text

    def invalidate(self, *, sandbox_id: str) -> None:
        with self._lock:
            self._entries.pop(sandbox_id, None)

    def _store(self, *, sandbox_id: str, text: str, now: float) -> None:
        if sandbox_id not in self._entries and len(self._entries) >= self.max_entries:
            # Evict the oldest-stored entry (insertion order ≈ store order since
            # a re-store pops-and-reinserts below).
            oldest = min(self._entries, key=lambda k: self._entries[k].stored_at)
            self._entries.pop(oldest, None)
        # Re-insert so dict order tracks recency for the eviction heuristic.
        self._entries.pop(sandbox_id, None)
        self._entries[sandbox_id] = _Entry(text=text, stored_at=now)
