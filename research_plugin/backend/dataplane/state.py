"""Worker-owned machine-local sandbox state (cloud plan §3.2).

Machine-local values — the sandbox SSH key path, the local sync dir,
daemon-owned loopback dashboard URLs, and the stable per-machine client
identity — must never live in cloud-bound rows. They live here instead, in a
small SQLite file under ``.research_plugin/`` owned by the data plane (the
daemon-mode successor is ``~/.research_plugin/daemon.sqlite``).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..utils import new_id


# Overrides the persisted client identity — for tests and for simulating a
# second client against one record store.
CLIENT_ID_ENV = "RESEARCH_PLUGIN_CLIENT_ID"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_local (
  experiment_id TEXT PRIMARY KEY,
  key_path TEXT NOT NULL DEFAULT '',
  local_sync_dir TEXT NOT NULL DEFAULT '',
  dashboards_local_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS client_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class SandboxLocalState:
    """Machine-local sandbox facts, keyed by experiment id or sandbox uid."""

    def __init__(self, *, db_path: Path) -> None:
        self.db_path = db_path
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        if not self._initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._initialized = True
        return conn

    def record(
        self,
        *,
        experiment_id: str,
        key_path: str | None = None,
        local_sync_dir: str | None = None,
        dashboards_local: dict[str, str] | None = None,
    ) -> None:
        """Upsert the provided fields; ``None`` leaves a field untouched."""
        fields: dict[str, Any] = {}
        if key_path is not None:
            fields["key_path"] = key_path
        if local_sync_dir is not None:
            fields["local_sync_dir"] = local_sync_dir
        if dashboards_local is not None:
            fields["dashboards_local_json"] = json.dumps(
                dashboards_local, sort_keys=True
            )
        if not fields:
            return
        assignments = ", ".join(f"{name} = excluded.{name}" for name in fields)
        columns = ", ".join(["experiment_id", *fields])
        placeholders = ", ".join("?" for _ in range(len(fields) + 1))
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO sandbox_local ({columns}) VALUES ({placeholders}) "
                    f"ON CONFLICT(experiment_id) DO UPDATE SET {assignments}",
                    [experiment_id, *fields.values()],
                )
        finally:
            conn.close()

    def load(self, *, experiment_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sandbox_local WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"key_path": "", "local_sync_dir": "", "dashboards_local": {}}
        return {
            "key_path": str(row["key_path"] or ""),
            "local_sync_dir": str(row["local_sync_dir"] or ""),
            "dashboards_local": _decode(row["dashboards_local_json"]),
        }

    def dashboards_local(self, *, experiment_id: str) -> dict[str, str]:
        return dict(self.load(experiment_id=experiment_id)["dashboards_local"])

    def client_id(self) -> str:
        """Stable per-machine data-plane identity — the sync-lease holder id.

        Generated once and persisted (cloud plan Phase 4); the
        ``RESEARCH_PLUGIN_CLIENT_ID`` env var overrides it for tests and
        multi-client simulations.
        """
        override = os.environ.get(CLIENT_ID_ENV, "").strip()
        if override:
            return override
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM client_meta WHERE key = 'client_id'"
            ).fetchone()
            if row is not None and str(row["value"] or ""):
                return str(row["value"])
            with conn:
                # OR IGNORE: a concurrent first boot keeps whichever id landed.
                conn.execute(
                    "INSERT OR IGNORE INTO client_meta (key, value) VALUES ('client_id', ?)",
                    (new_id(prefix="client"),),
                )
            row = conn.execute(
                "SELECT value FROM client_meta WHERE key = 'client_id'"
            ).fetchone()
            return str(row["value"])
        finally:
            conn.close()


def _decode(raw: Any) -> dict[str, str]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str) and v}
