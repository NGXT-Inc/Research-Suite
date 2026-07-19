"""Control-side transcript cursor cache (cloud plan Phase 9, risk 14).

Repeated transcript reads for the same sandbox within the TTL serve from memory
instead of re-hitting the src/merv/brain/SSH; a cursor that has consumed all cached
output reads fresh so new output is never hidden; the TTL and the bound expire
entries. Cursor comparisons use the transcript's TRUE byte size (not the tail
window length), so a log bigger than the window still refreshes correctly.
Clock-injectable so the test owns time.
"""

from __future__ import annotations

import unittest

from merv.brain.sandbox.sandbox_backend import TranscriptTail
from merv.brain.sandbox.transcript_cache import TranscriptCache


def _tail(text: str, *, total: int | None = None) -> TranscriptTail:
    data = text.encode("utf-8")
    return TranscriptTail(data=data, total_bytes=len(data) if total is None else total)


class TranscriptCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.cache = TranscriptCache(ttl_seconds=2.0, clock=lambda: self.now)
        self.reads = 0

    def _read(self, text: str, *, total: int | None = None):
        def read() -> TranscriptTail:
            self.reads += 1
            return _tail(text, total=total)

        return read

    def test_tail_poll_within_ttl_is_a_cache_hit(self) -> None:
        first = self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 1\n"))
        self.assertEqual(first, _tail("epoch 1\n"))
        self.assertEqual(self.reads, 1)
        # A second tail poll (no since) within the TTL serves from cache.
        second = self.cache.get_or_read(sandbox_id="sb1", read=self._read("SHOULD NOT READ"))
        self.assertEqual(second, _tail("epoch 1\n"))
        self.assertEqual(self.reads, 1)
        self.assertEqual(self.cache.hits, 1)

    def test_ttl_expiry_forces_a_fresh_read(self) -> None:
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 1\n"))
        self.now += 5.0  # past the 2 s TTL
        again = self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 2\n"))
        self.assertEqual(again, _tail("epoch 2\n"))
        self.assertEqual(self.reads, 2)

    def test_cursor_at_end_reads_fresh_even_within_ttl(self) -> None:
        # Caller has consumed all cached bytes (since == total): must read
        # fresh, so a fast-progressing sandbox's new output is never hidden by
        # the TTL.
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 1\n"))
        consumed = len("epoch 1\n")
        delta = self.cache.get_or_read(
            sandbox_id="sb1", read=self._read("epoch 1\nepoch 2\n"), since=consumed
        )
        self.assertEqual(delta, _tail("epoch 1\nepoch 2\n"))
        self.assertEqual(self.reads, 2)

    def test_cursor_into_cache_is_a_hit(self) -> None:
        # Many viewers at an EARLIER cursor coalesce onto one cached read.
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 1\nepoch 2\n"))
        served = self.cache.get_or_read(
            sandbox_id="sb1", read=self._read("SHOULD NOT READ"), since=2
        )
        self.assertEqual(served, _tail("epoch 1\nepoch 2\n"))
        self.assertEqual(self.reads, 1)
        self.assertEqual(self.cache.hits, 1)

    def test_cursor_compares_against_true_total_not_window_length(self) -> None:
        # A windowed entry: 8 window bytes of a 100-byte transcript. A cursor
        # past the window but inside the TRUE total is still a hit (the cached
        # window already covers it); a cursor at the true total reads fresh.
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 9\n", total=100))
        served = self.cache.get_or_read(
            sandbox_id="sb1", read=self._read("SHOULD NOT READ"), since=50
        )
        self.assertEqual(served, _tail("epoch 9\n", total=100))
        self.assertEqual(self.reads, 1)
        self.cache.get_or_read(
            sandbox_id="sb1", read=self._read("epoch 10\n", total=110), since=100
        )
        self.assertEqual(self.reads, 2)

    def test_fresh_flag_bypasses_cache(self) -> None:
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("a"))
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("b"), fresh=True)
        self.assertEqual(self.reads, 2)

    def test_empty_sandbox_id_never_cached(self) -> None:
        self.cache.get_or_read(sandbox_id="", read=self._read("x"))
        self.cache.get_or_read(sandbox_id="", read=self._read("y"))
        self.assertEqual(self.reads, 2)

    def test_bound_evicts_oldest(self) -> None:
        cache = TranscriptCache(ttl_seconds=100.0, max_entries=2, clock=lambda: self.now)
        cache.get_or_read(sandbox_id="a", read=lambda: _tail("a"))
        self.now += 1
        cache.get_or_read(sandbox_id="b", read=lambda: _tail("b"))
        self.now += 1
        cache.get_or_read(sandbox_id="c", read=lambda: _tail("c"))  # evicts "a"
        reads = {"n": 0}

        def read_a() -> TranscriptTail:
            reads["n"] += 1
            return _tail("a2")

        # "a" was evicted, so this reads fresh.
        self.assertEqual(cache.get_or_read(sandbox_id="a", read=read_a), _tail("a2"))
        self.assertEqual(reads["n"], 1)


if __name__ == "__main__":
    unittest.main()
