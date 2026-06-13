"""Control-side transcript cursor cache (cloud plan Phase 9, risk 14).

Repeated transcript reads for the same sandbox within the TTL serve from memory
instead of re-hitting the backend/SSH; a cursor that has consumed all cached
output reads fresh so new output is never hidden; the TTL and the bound expire
entries. Clock-injectable so the test owns time.
"""

from __future__ import annotations

import unittest

from backend.services.transcript_cache import TranscriptCache


class TranscriptCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.cache = TranscriptCache(ttl_seconds=2.0, clock=lambda: self.now)
        self.reads = 0

    def _read(self, text: str):
        def read() -> str:
            self.reads += 1
            return text

        return read

    def test_tail_poll_within_ttl_is_a_cache_hit(self) -> None:
        first = self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 1\n"))
        self.assertEqual(first, "epoch 1\n")
        self.assertEqual(self.reads, 1)
        # A second tail poll (no since) within the TTL serves from cache.
        second = self.cache.get_or_read(sandbox_id="sb1", read=self._read("SHOULD NOT READ"))
        self.assertEqual(second, "epoch 1\n")
        self.assertEqual(self.reads, 1)
        self.assertEqual(self.cache.hits, 1)

    def test_ttl_expiry_forces_a_fresh_read(self) -> None:
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 1\n"))
        self.now += 5.0  # past the 2 s TTL
        again = self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 2\n"))
        self.assertEqual(again, "epoch 2\n")
        self.assertEqual(self.reads, 2)

    def test_cursor_at_end_reads_fresh_even_within_ttl(self) -> None:
        # Caller has consumed all cached bytes (since == len): must read fresh,
        # so a fast-progressing sandbox's new output is never hidden by the TTL.
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 1\n"))
        consumed = len("epoch 1\n")
        delta = self.cache.get_or_read(
            sandbox_id="sb1", read=self._read("epoch 1\nepoch 2\n"), since=consumed
        )
        self.assertEqual(delta, "epoch 1\nepoch 2\n")
        self.assertEqual(self.reads, 2)

    def test_cursor_into_cache_is_a_hit(self) -> None:
        # Many viewers at an EARLIER cursor coalesce onto one cached read.
        self.cache.get_or_read(sandbox_id="sb1", read=self._read("epoch 1\nepoch 2\n"))
        served = self.cache.get_or_read(
            sandbox_id="sb1", read=self._read("SHOULD NOT READ"), since=2
        )
        self.assertEqual(served, "epoch 1\nepoch 2\n")
        self.assertEqual(self.reads, 1)
        self.assertEqual(self.cache.hits, 1)

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
        cache.get_or_read(sandbox_id="a", read=lambda: "a")
        self.now += 1
        cache.get_or_read(sandbox_id="b", read=lambda: "b")
        self.now += 1
        cache.get_or_read(sandbox_id="c", read=lambda: "c")  # evicts "a"
        reads = {"n": 0}

        def read_a() -> str:
            reads["n"] += 1
            return "a2"

        # "a" was evicted, so this reads fresh.
        self.assertEqual(cache.get_or_read(sandbox_id="a", read=read_a), "a2")
        self.assertEqual(reads["n"], 1)


if __name__ == "__main__":
    unittest.main()
