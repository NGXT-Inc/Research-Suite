from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone

from backend.utils import format_iso, parse_iso


class IsoTimestampTest(unittest.TestCase):
    def test_format_iso_normalizes_expected_shapes(self) -> None:
        self.assertEqual(
            format_iso(datetime(2026, 6, 21, 12, 0, 0, 123456, tzinfo=UTC)),
            "2026-06-21T12:00:00Z",
        )
        self.assertEqual(
            format_iso(datetime(2026, 6, 21, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))),
            "2026-06-21T12:00:00Z",
        )
        self.assertEqual(
            format_iso(datetime(2026, 6, 21, 12, 0, 0)),
            "2026-06-21T12:00:00Z",
        )

    def test_parse_iso_normalizes_expected_shapes(self) -> None:
        self.assertEqual(
            parse_iso("2026-06-21T12:00:00Z").isoformat(),
            "2026-06-21T12:00:00+00:00",
        )
        self.assertEqual(
            parse_iso("2026-06-21T12:00:00+00:00").isoformat(),
            "2026-06-21T12:00:00+00:00",
        )
        naive = parse_iso("2026-06-21T12:00:00")
        self.assertIsNotNone(naive)
        self.assertIs(naive.tzinfo, UTC)

    def test_parse_iso_returns_none_for_absent_or_invalid_values(self) -> None:
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso(""))
        self.assertIsNone(parse_iso("not-a-date"))


if __name__ == "__main__":
    unittest.main()
