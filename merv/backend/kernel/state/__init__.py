"""Record-store primitives and legacy local diagnostic adapters.

``StateStore`` is the local SQLite record store; hosted composition imports the
Postgres dialect directly. ``ActivityLogger`` and ``ToolCallStore`` remain
available to compatibility callers, while the current unified ``ControlApp``
uses bounded in-memory diagnostic sinks from ``control.control_runtime``.
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
