from __future__ import annotations

import unittest

from backend.domain.review_snapshot import review_snapshot_id


class ReviewSnapshotIdTest(unittest.TestCase):
    """Lock the load-bearing snapshot-id format.

    reviews.py compares this string for equality and parses it back
    positionally (snapshot_from_id), so field order and the resource-token
    shape must not drift.
    """

    def test_field_order_and_empty_resources_trailing_segment(self) -> None:
        target = {
            "id": "exp_1",
            "status": "running",
            "attempt_index": 2,
            "current_attempt_resources": [],
        }
        # `type|id|status|attempt|tokens`; empty tokens -> trailing empty seg.
        self.assertEqual(
            review_snapshot_id(target_type="experiment", target=target),
            "experiment|exp_1|running|2|",
        )

    def test_tokens_are_sorted(self) -> None:
        target = {
            "id": "syn_1",
            "status": "synthesizing",
            "attempt_index": 0,
            "current_attempt_resources": [
                {"id": "b", "version_token": "v1", "association_role": "doc",
                 "association_attempt_index": 0},
                {"id": "a", "version_token": "v1", "association_role": "doc",
                 "association_attempt_index": 0},
            ],
        }
        snap = review_snapshot_id(target_type="synthesis", target=target)
        self.assertEqual(
            snap,
            "synthesis|syn_1|synthesizing|0|a:v1:doc:0,b:v1:doc:0",
        )

    def test_association_version_id_wins_over_version_token(self) -> None:
        target = {
            "id": "exp_1",
            "status": "running",
            "attempt_index": 1,
            "current_attempt_resources": [
                {"id": "r1", "version_token": "tok",
                 "association_version_id": "rver_9",
                 "association_role": "code",
                 "association_attempt_index": 3},
            ],
        }
        snap = review_snapshot_id(target_type="experiment", target=target)
        self.assertEqual(snap, "experiment|exp_1|running|1|r1:rver_9:code:3")

    def test_falls_back_to_version_token(self) -> None:
        # No association_version_id (and the falsy-None case) -> version_token.
        target = {
            "id": "exp_1",
            "status": "running",
            "attempt_index": 0,
            "current_attempt_resources": [
                {"id": "r1", "version_token": "tok",
                 "association_version_id": None,
                 "association_role": "", "association_attempt_index": 0},
            ],
        }
        snap = review_snapshot_id(target_type="experiment", target=target)
        self.assertEqual(snap, "experiment|exp_1|running|0|r1:tok::0")


if __name__ == "__main__":
    unittest.main()
