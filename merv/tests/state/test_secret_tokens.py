from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from backend.secret_tokens import hash_secret, mint_secret, secret_digest_matches


class SecretTokensTest(unittest.TestCase):
    def test_mint_secret_applies_prefix_and_entropy_size(self) -> None:
        with patch("backend.secret_tokens.secrets.token_urlsafe", return_value="abc") as token_urlsafe:
            self.assertEqual(mint_secret(prefix="rpt_", nbytes=32), "rpt_abc")
        token_urlsafe.assert_called_once_with(32)

    def test_hash_secret_is_sha256_hex(self) -> None:
        self.assertEqual(
            hash_secret("rpt_secret"),
            hashlib.sha256(b"rpt_secret").hexdigest(),
        )

    def test_secret_digest_matches_by_constant_time_digest_compare(self) -> None:
        digest = hash_secret("rp_capability")

        self.assertTrue(
            secret_digest_matches(stored_digest=digest, presented_digest=digest)
        )
        self.assertFalse(
            secret_digest_matches(
                stored_digest=digest, presented_digest=hash_secret("wrong")
            )
        )

    def test_missing_stored_digest_still_burns_compare(self) -> None:
        digest = hash_secret("missing")
        with patch("backend.secret_tokens.hmac.compare_digest", return_value=True) as compare:
            self.assertFalse(
                secret_digest_matches(stored_digest=None, presented_digest=digest)
            )
        compare.assert_called_once_with(digest, digest)


if __name__ == "__main__":
    unittest.main()
