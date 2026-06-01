"""State / durability layer: SQLite store and activity log.

This layer owns the durable artifacts that survive a daemon restart:
  - StateStore: SQLite for projects, claims, experiments, resources, reviews,
    jobs, events, and reviewer capability tokens.
  - ActivityLogger: append-only JSONL event stream (and optional stderr mirror).
"""

from .activity import ActivityLogger, monotonic_ms
from .store import StateStore, row_to_dict, rows_to_dicts

__all__ = [
    "ActivityLogger",
    "StateStore",
    "monotonic_ms",
    "row_to_dict",
    "rows_to_dicts",
]
