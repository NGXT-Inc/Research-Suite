"""Full-fidelity tool-call recorder for the debug analyzer.

The activity log (`activity.py`) is a *bounded visibility feed*: it summarizes
arguments to IDs and caps results at 16 KB so it never balloons. That makes it
useless for the one thing debugging a context blow-up actually needs — reading
the *raw* request and response of a specific call.

`ToolCallStore` fills that gap. It keeps the FULL arguments and result of the
last `max_rows` tool calls in a dedicated SQLite file, so the UI can:

  - rank tools by how much they send back (with avg / p50 / p95 / max), and
  - drill into any single call and read the exact JSON the agent received.

Bounded by construction: a row cap (oldest evicted first) plus a per-payload
char cap (giant payloads stored as a truncation marker, while the recorded
size stays exact). It is local debug data, isolated in its own DB so churn and
the occasional `clear()` never touch the precious state DB.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from ..utils import now_iso
from .activity import SENSITIVE_KEYS, jsonable, payload_chars


DEFAULT_MAX_ROWS = 1500
# Per-payload display cap. Sizes (sent/received chars) are always recorded in
# full; only the stored *text* is capped, and only for the rare giant payload —
# 256 K chars is far more than anyone reads in a JSON viewer.
DEFAULT_MAX_PAYLOAD_CHARS = 256 * 1024

SORTABLE_COLUMNS = frozenset({"ts", "received_chars", "sent_chars", "duration_ms", "tool"})


class ToolCallStore:
    """SQLite-backed ring of full tool-call I/O, plus query/aggregate helpers."""

    def __init__(
        self,
        *,
        db_path: Path,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_payload_chars: int = DEFAULT_MAX_PAYLOAD_CHARS,
        enabled: bool = True,
    ) -> None:
        self.db_path = db_path
        self.max_rows = max_rows
        self.max_payload_chars = max_payload_chars
        self.enabled = enabled
        self._lock = threading.Lock()
        if self.enabled:
            self._init_db()

    # ---------- write path ----------

    def record(
        self,
        *,
        tool: str,
        source: str,
        status: str,
        duration_ms: int,
        arguments: dict[str, Any],
        result: Any | None = None,
        error: str = "",
        error_code: str = "",
    ) -> None:
        """Persist one tool call with its full I/O. Never raises (telemetry)."""
        if not self.enabled:
            return
        try:
            # Redact bearer secrets before they hit disk (cloud plan Phase 7):
            # this store keeps FULL arguments, so a reviewer capability passed to
            # review.start would otherwise sit in plaintext in tool_calls.sqlite.
            # Top-level keys only — the sensitive args are top-level fields.
            arguments = {
                key: ("[redacted]" if key in SENSITIVE_KEYS else value)
                for key, value in arguments.items()
            }
            args_text, args_trunc, sent_chars = self._encode(arguments)
            if status == "error":
                result_text = error or ""
                result_trunc = 0
                received_chars = len(result_text)
            else:
                result_text, result_trunc, received_chars = self._encode(result)
            with self._lock, self._db() as conn:
                conn.execute(
                    """
                    INSERT INTO tool_calls
                      (ts, tool, source, status, duration_ms, sent_chars, received_chars,
                       args_json, result_json, error_code, args_truncated, result_truncated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_iso(), tool, source, status, int(duration_ms or 0),
                        sent_chars, received_chars, args_text, result_text,
                        error_code, args_trunc, result_trunc,
                    ),
                )
                conn.execute(
                    "DELETE FROM tool_calls WHERE id <= "
                    "(SELECT MAX(id) FROM tool_calls) - ?",
                    (self.max_rows,),
                )
        except Exception:  # noqa: BLE001 — debug recording must never break a call
            pass

    # ---------- read path ----------

    def stats(
        self,
        *,
        minutes: int | None = None,
        source: str | None = None,
        status: str | None = None,
        tool: str | None = None,
        limit: int = 200,
        sort: str = "ts",
        order: str = "desc",
    ) -> dict[str, Any]:
        """Per-tool aggregate + a filtered/sorted slice of individual calls.

        Filters (time window, source, status, tool substring) are applied first;
        the per-tool aggregate and the call rows both describe that same filtered
        set. Aggregates include avg / p50 / p95 / max received so a tool that is
        *occasionally* huge is distinguishable from one that is *consistently*
        large. Call rows are lightweight (no payloads) — fetch a single call's
        full I/O with `get()`.
        """
        limit = max(1, min(int(limit), 2000))
        sort = sort if sort in SORTABLE_COLUMNS else "ts"
        reverse = str(order).lower() != "asc"
        empty_totals = {"calls": 0, "sent_chars": 0, "received_chars": 0, "error_calls": 0}
        base = {
            "calls": [],
            "by_tool": [],
            "totals": dict(empty_totals),
            "coverage": {"calls": 0, "stored": 0, "oldest_ts": None, "newest_ts": None, "capped": False},
            "filter": {"minutes": minutes, "source": source, "status": status, "tool": tool},
        }
        if not self.enabled or not self.db_path.exists():
            return base

        where, params = self._build_where(minutes=minutes, source=source, status=status, tool=tool)
        with self._lock, self._db() as conn:
            rows = conn.execute(
                f"SELECT id, ts, tool, source, status, duration_ms, sent_chars, "
                f"received_chars, error_code FROM tool_calls{where}",
                params,
            ).fetchall()
            stored = conn.execute("SELECT COUNT(*) AS n, MIN(ts) AS o FROM tool_calls").fetchone()

        calls = [dict(r) for r in rows]
        totals = dict(empty_totals)
        agg: dict[str, dict[str, Any]] = {}
        for c in calls:
            totals["calls"] += 1
            totals["sent_chars"] += c["sent_chars"]
            totals["received_chars"] += c["received_chars"]
            if c["status"] == "error":
                totals["error_calls"] += 1
            self._accumulate(agg, c)
        by_tool = sorted(
            (self._finalize_bucket(b) for b in agg.values()),
            key=lambda b: b["received_chars"],
            reverse=True,
        )

        calls.sort(key=lambda c: (c.get(sort) if c.get(sort) is not None else 0), reverse=reverse)
        oldest = min((c["ts"] for c in calls), default=None)
        newest = max((c["ts"] for c in calls), default=None)
        capped = self._coverage_capped(stored=stored, minutes=minutes)
        return {
            "calls": calls[:limit],
            "by_tool": by_tool,
            "totals": totals,
            "coverage": {
                "calls": totals["calls"],
                "stored": stored["n"] if stored else 0,
                "oldest_ts": oldest,
                "newest_ts": newest,
                "capped": capped,
            },
            "filter": {"minutes": minutes, "source": source, "status": status, "tool": tool},
        }

    def get(self, *, call_id: int) -> dict[str, Any] | None:
        """Return one call's full record, with args/result parsed back to JSON."""
        if not self.enabled or not self.db_path.exists():
            return None
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT * FROM tool_calls WHERE id = ?", (call_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["args"] = _safe_load(record.pop("args_json", ""))
        if record.get("status") == "error":
            # Errors store a plain message, not JSON.
            record["result"] = record.pop("result_json", "") or ""
        else:
            record["result"] = _safe_load(record.pop("result_json", ""))
        record["args_truncated"] = bool(record.get("args_truncated"))
        record["result_truncated"] = bool(record.get("result_truncated"))
        return record

    def clear(self) -> dict[str, Any]:
        """Drop all recorded calls. Returns how many were removed."""
        if not self.enabled or not self.db_path.exists():
            return {"cleared": 0}
        with self._lock, self._db() as conn:
            before = conn.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
            conn.execute("DELETE FROM tool_calls")
        return {"cleared": before}

    # ---------- internals ----------

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and always closes.

        `with sqlite3.connect(...)` only manages the transaction, not the handle,
        so without this the per-call connections would leak file descriptors.
        """
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_calls (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL,
                  tool TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT 'mcp',
                  status TEXT NOT NULL DEFAULT 'ok',
                  duration_ms INTEGER NOT NULL DEFAULT 0,
                  sent_chars INTEGER NOT NULL DEFAULT 0,
                  received_chars INTEGER NOT NULL DEFAULT 0,
                  args_json TEXT NOT NULL DEFAULT '',
                  result_json TEXT NOT NULL DEFAULT '',
                  error_code TEXT NOT NULL DEFAULT '',
                  args_truncated INTEGER NOT NULL DEFAULT 0,
                  result_truncated INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool)")

    def _encode(self, value: Any) -> tuple[str, int, int]:
        """Serialize a payload for storage. Returns (text, truncated, full_chars).

        `full_chars` is the true serialized size — recorded even when the stored
        text is replaced by a truncation marker, so size stats stay exact.
        """
        chars = payload_chars(value=value)
        try:
            text = json.dumps(jsonable(value=value), sort_keys=True)
        except (TypeError, ValueError):
            text = json.dumps(str(value))
        if len(text) > self.max_payload_chars:
            marker = json.dumps(
                {"_truncated": True, "_chars": chars, "preview": text[: self.max_payload_chars]}
            )
            return marker, 1, chars
        return text, 0, chars

    @staticmethod
    def _build_where(
        *, minutes: int | None, source: str | None, status: str | None, tool: str | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if minutes and minutes > 0:
            cutoff = (datetime.now(tz=UTC) - timedelta(minutes=minutes)).replace(microsecond=0)
            clauses.append("ts >= ?")
            params.append(cutoff.isoformat().replace("+00:00", "Z"))
        if source and source != "all":
            clauses.append("source = ?")
            params.append(source)
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if tool:
            clauses.append("tool LIKE ?")
            params.append(f"%{tool}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    @staticmethod
    def _accumulate(agg: dict[str, dict[str, Any]], call: dict[str, Any]) -> None:
        bucket = agg.setdefault(
            call["tool"],
            {
                "tool": call["tool"],
                "calls": 0,
                "error_calls": 0,
                "sent_chars": 0,
                "received_chars": 0,
                "max_received_chars": 0,
                "max_sent_chars": 0,
                "_dur_sum": 0,
                "max_duration_ms": 0,
                "_recv_samples": [],
                "last_ts": None,
            },
        )
        bucket["calls"] += 1
        bucket["error_calls"] += 1 if call["status"] == "error" else 0
        bucket["sent_chars"] += call["sent_chars"]
        bucket["received_chars"] += call["received_chars"]
        bucket["max_received_chars"] = max(bucket["max_received_chars"], call["received_chars"])
        bucket["max_sent_chars"] = max(bucket["max_sent_chars"], call["sent_chars"])
        bucket["_dur_sum"] += call["duration_ms"]
        bucket["max_duration_ms"] = max(bucket["max_duration_ms"], call["duration_ms"])
        bucket["_recv_samples"].append(call["received_chars"])
        if bucket["last_ts"] is None or call["ts"] > bucket["last_ts"]:
            bucket["last_ts"] = call["ts"]

    @staticmethod
    def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
        count = bucket["calls"] or 1
        samples = sorted(bucket.pop("_recv_samples"))
        bucket["avg_received_chars"] = round(bucket["received_chars"] / count)
        bucket["avg_sent_chars"] = round(bucket["sent_chars"] / count)
        bucket["avg_duration_ms"] = round(bucket.pop("_dur_sum") / count)
        bucket["p50_received_chars"] = _percentile(samples, 50)
        bucket["p95_received_chars"] = _percentile(samples, 95)
        return bucket

    def _coverage_capped(self, *, stored: sqlite3.Row | None, minutes: int | None) -> bool:
        """True when the requested window may extend past evicted history."""
        if stored is None or stored["n"] < self.max_rows:
            return False
        if not minutes or minutes <= 0:
            return True  # "all", but the ring is full → older calls were evicted
        cutoff = datetime.now(tz=UTC) - timedelta(minutes=minutes)
        oldest = _parse_ts(stored["o"])
        # Ring is full AND its oldest stored call is still inside the window →
        # there may be matching calls that were already evicted.
        return oldest is None or oldest >= cutoff


def _percentile(sorted_values: list[int], q: int) -> int:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(1, -(-q * len(sorted_values) // 100))  # ceil(q/100 * n)
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _safe_load(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text
