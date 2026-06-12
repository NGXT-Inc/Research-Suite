"""State / durability layer: SQLite store and activity log.

This layer owns the durable artifacts that survive a daemon restart:
  - StateStore: SQLite for projects, claims, experiments, resources, reviews,
    jobs, events, and reviewer capability tokens.
  - ActivityLogger: append-only JSONL event stream (and optional stderr mirror).
  - ToolCallStore: bounded SQLite ring of full tool-call I/O for the debug view.
"""

from .activity import ActivityLogger, monotonic_ms
from .store import BaseStateStore, SqliteStateStore, StateStore, row_to_dict, rows_to_dicts
from .tool_calls import ToolCallStore

# The Postgres dialect (state.dialects.PostgresStateStore) is deliberately
# not re-exported here: importing it is a control-profile/test concern and
# its psycopg dependency must stay optional for local installs.

__all__ = [
    "ActivityLogger",
    "BaseStateStore",
    "SqliteStateStore",
    "StateStore",
    "ToolCallStore",
    "monotonic_ms",
    "row_to_dict",
    "rows_to_dicts",
]
