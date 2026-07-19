from __future__ import annotations

import os
import unittest
from unittest import mock

from merv.brain.kernel.env import (
    _reset_env_deprecation_warnings,
    env_bool,
    env_float,
    env_int,
    env_name_pair,
    env_raw,
    env_value,
    merv_env_name,
)
from merv.shared.client_config import (
    dual_env_value,
    _warned_legacy_names as _shared_warned,
)


class EnvNamePairTest(unittest.TestCase):
    def test_pair_from_either_spelling(self) -> None:
        self.assertEqual(
            env_name_pair("MERV_MODE"), ("MERV_MODE", "RESEARCH_PLUGIN_MODE")
        )
        self.assertEqual(
            env_name_pair("RESEARCH_PLUGIN_MODE"),
            ("MERV_MODE", "RESEARCH_PLUGIN_MODE"),
        )
        self.assertEqual(merv_env_name("RESEARCH_PLUGIN_DB_URL"), "MERV_DB_URL")

    def test_unprefixed_names_pass_through(self) -> None:
        self.assertEqual(env_name_pair("AWS_REGION"), ("AWS_REGION", "AWS_REGION"))


class DualReadPrecedenceTest(unittest.TestCase):
    def test_primary_wins_over_legacy(self) -> None:
        env = {"MERV_DB_URL": "new", "RESEARCH_PLUGIN_DB_URL": "old"}
        self.assertEqual(env_value("MERV_DB_URL", env=env), "new")
        self.assertEqual(env_value("RESEARCH_PLUGIN_DB_URL", env=env), "new")

    def test_legacy_fallback_when_primary_unset(self) -> None:
        env = {"RESEARCH_PLUGIN_DB_URL": "old"}
        self.assertEqual(env_value("MERV_DB_URL", env=env), "old")
        self.assertEqual(env_value("RESEARCH_PLUGIN_DB_URL", env=env), "old")

    def test_empty_primary_is_unset_so_legacy_wins(self) -> None:
        env = {"MERV_DB_URL": "  ", "RESEARCH_PLUGIN_DB_URL": "old"}
        self.assertEqual(env_value("MERV_DB_URL", env=env), "old")

    def test_both_empty_or_missing_resolve_to_none(self) -> None:
        self.assertIsNone(env_value("MERV_DB_URL", env={}))
        self.assertIsNone(
            env_value("MERV_DB_URL", env={"RESEARCH_PLUGIN_DB_URL": " "})
        )

    def test_env_raw_preserves_set_but_blank(self) -> None:
        self.assertEqual(env_raw("MERV_DB_URL", env={"MERV_DB_URL": " "}), "")
        self.assertEqual(
            env_raw("MERV_DB_URL", env={"RESEARCH_PLUGIN_DB_URL": ""}), ""
        )
        self.assertIsNone(env_raw("MERV_DB_URL", env={}))

    def test_coercion_helpers_accept_either_spelling(self) -> None:
        env = {"MERV_SANDBOX_REAPER": "0"}
        self.assertFalse(env_bool("RESEARCH_PLUGIN_SANDBOX_REAPER", True, env=env))
        env = {"RESEARCH_PLUGIN_HTTP_PORT": "9999"}
        self.assertEqual(env_int("MERV_HTTP_PORT", 8787, env=env), 9999)
        env = {"MERV_SANDBOX_STALE": "12.5", "RESEARCH_PLUGIN_SANDBOX_STALE": "1"}
        self.assertEqual(
            env_float("RESEARCH_PLUGIN_SANDBOX_STALE", None, 3.0, env=env), 12.5
        )


class DeprecationWarningTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_env_deprecation_warnings()
        self.addCleanup(_reset_env_deprecation_warnings)

    def test_legacy_source_from_process_env_warns_once(self) -> None:
        with mock.patch.dict(os.environ, {"RESEARCH_PLUGIN_BLOB_BUCKET": "b"}):
            os.environ.pop("MERV_BLOB_BUCKET", None)
            with self.assertLogs("merv.brain.kernel.env", level="WARNING") as logs:
                self.assertEqual(env_value("MERV_BLOB_BUCKET"), "b")
            self.assertEqual(len(logs.output), 1)
            self.assertIn("RESEARCH_PLUGIN_BLOB_BUCKET is deprecated", logs.output[0])
            self.assertIn("MERV_BLOB_BUCKET", logs.output[0])
            # Second read of the same variable stays silent.
            with self.assertNoLogs("merv.brain.kernel.env", level="WARNING"):
                self.assertEqual(env_value("MERV_BLOB_BUCKET"), "b")

    def test_primary_source_never_warns(self) -> None:
        env_vars = {"MERV_BLOB_BUCKET": "b", "RESEARCH_PLUGIN_BLOB_BUCKET": "old"}
        with mock.patch.dict(os.environ, env_vars):
            with self.assertNoLogs("merv.brain.kernel.env", level="WARNING"):
                self.assertEqual(env_value("MERV_BLOB_BUCKET"), "b")

    def test_explicit_env_mapping_never_warns(self) -> None:
        with self.assertNoLogs("merv.brain.kernel.env", level="WARNING"):
            env_value("MERV_BLOB_BUCKET", env={"RESEARCH_PLUGIN_BLOB_BUCKET": "b"})


class SharedDualEnvValueTest(unittest.TestCase):
    """The stdlib-only twin in merv.shared matches merv.brain.kernel.env."""

    def setUp(self) -> None:
        _shared_warned.clear()
        self.addCleanup(_shared_warned.clear)

    def test_precedence_matches_backend_resolver(self) -> None:
        env = {"MERV_CONTROL_URL": "new", "RESEARCH_PLUGIN_CONTROL_URL": "old"}
        self.assertEqual(dual_env_value("MERV_CONTROL_URL", env), "new")
        self.assertEqual(
            dual_env_value("MERV_CONTROL_URL", {"RESEARCH_PLUGIN_CONTROL_URL": "old"}),
            "old",
        )
        self.assertIsNone(dual_env_value("MERV_CONTROL_URL", {"MERV_CONTROL_URL": " "}))

    def test_legacy_process_env_source_warns_once_on_stderr(self) -> None:
        import contextlib
        import io

        with mock.patch.dict(os.environ, {"RESEARCH_PLUGIN_API_KEY": "rr_sk_x"}):
            os.environ.pop("MERV_API_KEY", None)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(dual_env_value("MERV_API_KEY"), "rr_sk_x")
                self.assertEqual(dual_env_value("MERV_API_KEY"), "rr_sk_x")
            output = stderr.getvalue()
            self.assertEqual(output.count("RESEARCH_PLUGIN_API_KEY is deprecated"), 1)
            self.assertIn("MERV_API_KEY", output)


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
