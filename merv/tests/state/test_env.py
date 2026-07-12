from __future__ import annotations

import unittest

from backend.env import env_bool, env_float, env_int


class EnvCoercionTest(unittest.TestCase):
    def test_env_bool_uses_default_for_missing_or_blank(self) -> None:
        self.assertTrue(env_bool("FLAG", default=True, env={}))
        self.assertFalse(env_bool("FLAG", default=False, env={"FLAG": "  "}))

    def test_env_bool_common_false_values_disable(self) -> None:
        for value in ("0", "false", "FALSE", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(env_bool("FLAG", default=True, env={"FLAG": value}))

    def test_env_bool_non_empty_non_false_values_enable(self) -> None:
        for value in ("1", "true", "yes", "on", "enabled"):
            with self.subTest(value=value):
                self.assertTrue(env_bool("FLAG", default=False, env={"FLAG": value}))

    def test_env_float_prefers_override_then_env_then_default(self) -> None:
        self.assertEqual(env_float("SECONDS", 2.5, 1.0, env={"SECONDS": "4"}), 2.5)
        self.assertEqual(env_float("SECONDS", None, 1.0, env={"SECONDS": "4"}), 4.0)
        self.assertEqual(env_float("SECONDS", None, 1.0, env={}), 1.0)
        self.assertEqual(env_float("SECONDS", None, 1.0, env={"SECONDS": "bad"}), 1.0)

    def test_env_float_strict_rejects_invalid_env_value(self) -> None:
        with self.assertRaises(ValueError):
            env_float("SECONDS", None, 1.0, env={"SECONDS": "bad"}, strict=True)

    def test_env_int_uses_default_and_strictness(self) -> None:
        self.assertEqual(env_int("COUNT", 3, env={}), 3)
        self.assertEqual(env_int("COUNT", 3, env={"COUNT": "5"}), 5)
        with self.assertRaises(ValueError):
            env_int("COUNT", 3, env={"COUNT": "bad"})
        self.assertEqual(env_int("COUNT", 3, env={"COUNT": "bad"}, strict=False), 3)


if __name__ == "__main__":
    unittest.main()
