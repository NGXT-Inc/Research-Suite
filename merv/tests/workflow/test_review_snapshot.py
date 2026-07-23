from __future__ import annotations

import unittest

from merv.brain.research_core.domain.review_snapshot import (
    review_snapshot_id,
    snapshot_from_id,
)


class ReviewSnapshotIdTest(unittest.TestCase):
    """Lock the load-bearing snapshot-id format.

    reviews.py compares this string for equality and parses it back
    positionally (snapshot_from_id), so field order and the artifact-token
    shape (artifact_id:role:attempt) must not drift.
    """

    def test_field_order_and_empty_resources_trailing_segment(self) -> None:
        target = {
            "id": "exp_1",
            "status": "running",
            "attempt_index": 2,
            "current_attempt_artifacts": [],
        }
        # `type|id|status|attempt|tokens`; empty tokens -> trailing empty seg.
        self.assertEqual(
            review_snapshot_id(target_type="experiment", target=target),
            "experiment|exp_1|running|2|",
        )

    def test_tokens_are_sorted(self) -> None:
        target = {
            "id": "ref_1",
            "status": "synthesizing",
            "attempt_index": 0,
            "current_attempt_artifacts": [
                {"id": "art_b", "role": "reflection_doc",
                 "attempt_index": 0},
                {"id": "art_a", "role": "reflection_doc",
                 "attempt_index": 0},
            ],
        }
        snap = review_snapshot_id(target_type="reflection", target=target)
        self.assertEqual(
            snap,
            "reflection|ref_1|synthesizing|0|"
            "art_a:reflection_doc:0,art_b:reflection_doc:0",
        )

    def test_resubmission_changes_the_snapshot(self) -> None:
        # A resubmit mints a NEW artifact id in the same slot, so the id — not
        # any content fingerprint — is what invalidates a pinned review.
        base = {
            "id": "exp_1",
            "status": "experiment_review",
            "attempt_index": 1,
        }
        before = review_snapshot_id(
            target_type="experiment",
            target={**base, "current_attempt_artifacts": [
                {"id": "art_1", "role": "report",
                 "attempt_index": 1},
            ]},
        )
        after = review_snapshot_id(
            target_type="experiment",
            target={**base, "current_attempt_artifacts": [
                {"id": "art_2", "role": "report",
                 "attempt_index": 1},
            ]},
        )
        self.assertNotEqual(before, after)

    def test_snapshot_round_trips_artifact_tokens(self) -> None:
        target = {
            "id": "exp_1",
            "status": "running",
            "attempt_index": 3,
            "current_attempt_artifacts": [
                {"id": "art_9", "role": "plan",
                 "attempt_index": 3},
            ],
        }
        snap = review_snapshot_id(target_type="experiment", target=target)
        parsed = snapshot_from_id(snapshot_id=snap)
        self.assertEqual(parsed["target_type"], "experiment")
        self.assertEqual(parsed["target_id"], "exp_1")
        self.assertEqual(parsed["attempt_index"], 3)
        self.assertEqual(
            parsed["artifacts"],
            [{"artifact_id": "art_9", "role": "plan", "attempt_index": 3}],
        )


if __name__ == "__main__":
    unittest.main()
