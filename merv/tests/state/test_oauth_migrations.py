"""Append-only schema migration coverage for the OAuth Phase-B tables.

Migrations 28-30 create the three surface-owned OAuth tables from the same
SCHEMA-extracted DDL that a fresh database gets, gated on ``_has_table`` so an
existing store is upgraded in place. The audience + oauth_family_id columns
these access bearers ride live in migration 26's DDL, so no separate column
migration appears here.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from merv.brain.kernel.state.store import MIGRATIONS, StateStore


class OAuthMigrationTest(unittest.TestCase):
    def test_fresh_schema_has_surface_owned_oauth_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(db_path=Path(tmp) / "state.sqlite")
            with store.connect() as conn:
                columns = {
                    table: {
                        row["name"]
                        for row in conn.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                    for table in (
                        "oauth_clients",
                        "oauth_authorization_codes",
                        "oauth_refresh_tokens",
                    )
                }
                migrations = [
                    (row["version"], row["name"])
                    for row in conn.execute(
                        "SELECT version, name FROM schema_migrations "
                        "WHERE version >= 28 ORDER BY version"
                    ).fetchall()
                ]
        self.assertEqual(
            columns["oauth_clients"],
            {
                "client_id",
                "client_name",
                "redirect_uris_json",
                "grant_types_json",
                "created_at",
            },
        )
        self.assertEqual(
            columns["oauth_authorization_codes"],
            {
                "code_digest",
                "client_id",
                "redirect_uri",
                "owner_user_id",
                "project_id",
                "code_challenge",
                "resource",
                "created_at",
                "expires_at",
                "consumed_at",
            },
        )
        self.assertEqual(
            columns["oauth_refresh_tokens"],
            {
                "id",
                "family_id",
                "secret_digest",
                "client_id",
                "owner_user_id",
                "project_id",
                "resource",
                "current_key_id",
                "parent_token_id",
                "created_at",
                "expires_at",
                "consumed_at",
                "revoked_at",
            },
        )
        self.assertEqual(
            migrations,
            [
                (28, "add_oauth_clients"),
                (29, "add_oauth_authorization_codes"),
                (30, "add_oauth_refresh_tokens"),
            ],
        )
        self.assertEqual(
            [(version, name) for version, name, _statement in MIGRATIONS[-3:]],
            migrations,
        )

    def test_v28_through_v30_upgrade_an_existing_store_without_rewriting_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            StateStore(db_path=db_path)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("DROP TABLE oauth_refresh_tokens")
                conn.execute("DROP TABLE oauth_authorization_codes")
                conn.execute("DROP TABLE oauth_clients")
                conn.execute("DELETE FROM schema_migrations WHERE version >= 28")
                before = conn.execute(
                    "SELECT version, name FROM schema_migrations ORDER BY version"
                ).fetchall()
                conn.commit()
            finally:
                conn.close()

            migrated = StateStore(db_path=db_path)
            with migrated.connect() as conn:
                after = [
                    (row["version"], row["name"])
                    for row in conn.execute(
                        "SELECT version, name FROM schema_migrations ORDER BY version"
                    ).fetchall()
                ]
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
        self.assertEqual(
            before,
            [(version, name) for version, name, _statement in MIGRATIONS[:-3]],
        )
        self.assertEqual(
            after,
            [(version, name) for version, name, _statement in MIGRATIONS],
        )
        self.assertTrue(
            {
                "oauth_clients",
                "oauth_authorization_codes",
                "oauth_refresh_tokens",
            }
            <= tables
        )


if __name__ == "__main__":
    unittest.main()
