from __future__ import annotations

import unittest

from backend.domain.synopsis import validate_synopsis

VALID = (
    "The embedding-initialized head narrowly beat its rerun baseline, so the "
    "claim holds in scope, but the older stronger setup still wins overall."
)


class SynopsisPolicyTest(unittest.TestCase):
    def test_accepts_a_plain_sentence_and_strips_whitespace(self) -> None:
        self.assertEqual(validate_synopsis(f"  {VALID}  "), VALID)

    def test_too_short_is_rejected_with_instructive_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "researcher's TLDR"):
            validate_synopsis("Too short.")

    def test_too_long_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "researcher's TLDR"):
            validate_synopsis("x" * 421)

    def test_newline_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "newline"):
            validate_synopsis(VALID + "\nSecond line.")

    def test_backtick_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "backtick"):
            validate_synopsis(VALID.replace("head", "`head`"))

    def test_markdown_heading_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "markdown"):
            validate_synopsis("# " + VALID)

    def test_entity_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "entity ids"):
            validate_synopsis(
                "exp_3f2a val_bpb=1.037680 vs anchor 1.038715, verdict pass "
                "on the rerun baseline for this attempt."
            )

    def test_entity_id_prefixes_all_rejected(self) -> None:
        for prefix in ("exp", "claim", "res", "rev", "rver", "syn"):
            with self.assertRaisesRegex(ValueError, "entity ids"):
                validate_synopsis(f"{VALID} Reference {prefix}_abc123 here.")

    def test_prefix_like_word_without_id_shape_is_allowed(self) -> None:
        # "expression" and "review" are ordinary words, not entity ids — the
        # regex requires the underscore + alphanumeric id tail.
        validate_synopsis(
            "The expression of interest in the review process was strong "
            "across the board, and the claim holds up under scrutiny today."
        )


if __name__ == "__main__":
    unittest.main()
